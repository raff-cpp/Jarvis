"""Server di Jarvis: Flask + WebSocket, serve la HUD e lancia Chrome in app-mode.

Il server ascolta SOLO su 127.0.0.1: non e raggiungibile dalla rete locale.
La chiave di Google AI Studio resta in questo processo e non viene mai
inviata al browser.
"""
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser

from flask import Flask, Response, jsonify, request, send_from_directory
from flask_sock import Sock

# Carica la .env (GEMINI_API_KEY, JARVIS_MODEL, ...) prima di importare il brain.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

import speech                    # noqa: E402
import tools                     # noqa: E402
from brain import Jarvis, redact  # noqa: E402

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PORT = int(os.environ.get("JARVIS_PORT", "8730"))

app = Flask(__name__, static_folder=WEB, static_url_path="")
sock = Sock(app)
jarvis = Jarvis()

# Una sola conversazione condivisa: serializza i comandi cosi due richieste
# concorrenti non si intrecciano nella stessa cronologia.
_brain_lock = threading.Lock()

# Connessioni HUD aperte: servono per spingere messaggi dal server al browser
# (finora il traffico andava solo nell'altro verso).
_clients = set()
_clients_lock = threading.Lock()


def _broadcast(obj):
    """Manda un messaggio a tutte le HUD collegate."""
    data = json.dumps(obj)
    with _clients_lock:
        morte = []
        for c in _clients:
            try:
                c.send(data)
            except Exception:
                morte.append(c)
        for c in morte:
            _clients.discard(c)


@app.after_request
def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/")
def index():
    return send_from_directory(WEB, "index.html")


@app.route("/api/info")
def info():
    # Espone solo booleani e il nome del modello: MAI la chiave.
    return jsonify({"has_key": jarvis.has_key, "model": jarvis.model,
                    "server_tts": speech.available})


@app.route("/api/voices")
def voices():
    """Voci neurali disponibili lato server (qualita molto superiore al browser)."""
    if not speech.available:
        return jsonify({"voices": []})
    etichette = {
        "diego": "Diego — maschile, autorevole",
        "giuseppe": "Giuseppe — maschile, morbido",
        "elsa": "Elsa — femminile",
        "isabella": "Isabella — femminile",
    }
    return jsonify({"default": speech.DEFAULT_VOICE,
                    "voices": [{"id": k, "label": etichette.get(k, k)}
                               for k in speech.VOICES]})


@app.route("/api/tts", methods=["POST"])
def tts():
    """Sintetizza il testo con la voce neurale e restituisce un MP3."""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "testo mancante"}), 400
    try:
        audio = speech.synthesize(text, data.get("voice"))
    except Exception as e:
        # La HUD ripiega da sola sulla voce del browser.
        print("[jarvis] sintesi vocale non disponibile: " + redact(e), file=sys.stderr)
        return jsonify({"error": "sintesi non disponibile"}), 503
    return Response(audio, mimetype="audio/mpeg",
                    headers={"Cache-Control": "no-store"})


@app.route("/api/stats")
def stats():
    return jsonify(tools.system_status_brief())


@app.route("/api/reset", methods=["POST"])
def reset():
    with _brain_lock:
        jarvis.reset()
    return jsonify({"status": "ok"})


@sock.route("/ws")
def ws(conn):
    with _clients_lock:
        _clients.add(conn)
    try:
        _ws_loop(conn)
    finally:
        with _clients_lock:
            _clients.discard(conn)


def _ws_loop(conn):
    while True:
        raw = conn.receive()
        if raw is None:
            break
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            continue

        def send(obj):
            try:
                conn.send(json.dumps(obj))
            except Exception:
                pass

        # I gesti non passano dal modello: sono azioni dirette e devono essere
        # immediate (il pinch aggiorna il volume molte volte al secondo).
        if msg.get("type") == "gesture":
            esito = _handle_gesture(msg)
            if esito:
                send({"type": "gesture_ack", "text": esito})
            continue

        if msg.get("type") != "command":
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue

        try:
            with _brain_lock:
                jarvis.process(
                    text,
                    on_status=lambda s: send({"type": "status", "state": s}),
                    on_action=lambda a: send({"type": "action", "text": a}),
                    on_say=lambda t: send({"type": "say", "text": t}),
                )
        except Exception as e:
            send({"type": "status", "state": "error"})
            send({"type": "say", "text": "Si e verificato un errore interno, signore."})
            # redact(): la chiave non deve finire nemmeno nei log di console.
            print("[jarvis] errore nel turno: " + redact(e), file=sys.stderr)
        send({"type": "done"})


def _handle_gesture(msg):
    """Esegue un gesto. Ritorna un testo per la HUD, o None se silenzioso."""
    action = (msg.get("action") or "").strip().lower()
    value = msg.get("value")
    try:
        if action == "volume":
            tools.set_volume(level=int(value))
            return None                      # continuo: non intasare il registro
        if action == "media":
            verso = "next" if value == "next" else "previous"
            tools.media_control(verso)
            return "Traccia successiva" if verso == "next" else "Traccia precedente"
        if action == "window":
            tools.window_to_monitor("right" if value == "right" else "left")
            return "Finestra spostata"
        if action == "mute":
            tools.set_volume(mute=True)
            return "Audio commutato"
    except Exception as e:
        print("[jarvis] gesto non riuscito: " + redact(e), file=sys.stderr)
        return "Gesto non riuscito"
    return None


# ---- Scorciatoia globale ---------------------------------------------------
# Senza, per parlare a Jarvis bisogna prima andare a cercare la sua finestra.
# Con Ctrl+Alt+J lo richiami da qualunque applicazione: la HUD viene in primo
# piano e il microfono si apre gia armato.

HOTKEY = os.environ.get("JARVIS_HOTKEY", "ctrl+alt+j").strip().lower()

_VK = {**{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
       "space": 0x20, "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
       "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78,
       "f10": 0x79, "f11": 0x7A, "f12": 0x7B}
_MOD = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002,
        "shift": 0x0004, "win": 0x0008}


def _parse_hotkey(spec):
    """'ctrl+alt+j' -> (modificatori, tasto). None se non interpretabile."""
    mods, tasto = 0, None
    for pezzo in spec.split("+"):
        pezzo = pezzo.strip()
        if pezzo in _MOD:
            mods |= _MOD[pezzo]
        elif pezzo in _VK:
            tasto = _VK[pezzo]
    return (mods, tasto) if mods and tasto else None


def _on_hotkey():
    """Porta la HUD in primo piano e apre il microfono."""
    try:
        # Il worker PowerShell e gia acceso: la finestra viene su all'istante.
        tools._worker.send(
            "$w = New-Object -ComObject WScript.Shell; $null = $w.AppActivate('J.A.R.V.I.S.')")
    except Exception:
        pass
    _broadcast({"type": "wake"})


def _hotkey_loop():
    import ctypes
    from ctypes import wintypes

    combo = _parse_hotkey(HOTKEY)
    if not combo:
        print(f"[jarvis] scorciatoia '{HOTKEY}' non interpretabile, ignorata.", file=sys.stderr)
        return
    mods, tasto = combo
    user32 = ctypes.windll.user32
    MOD_NOREPEAT = 0x4000
    # RegisterHotKey e il ciclo dei messaggi devono stare nello STESSO thread.
    if not user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, tasto):
        print(f"[jarvis] scorciatoia {HOTKEY.upper()} non disponibile "
              "(probabilmente gia usata da un altro programma).", file=sys.stderr)
        return
    print(f"  Scorciatoia globale: {HOTKEY.upper()}")
    msg = wintypes.MSG()
    WM_HOTKEY = 0x0312
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY:
            _on_hotkey()


def _port_in_use():
    """True se qualcosa ascolta gia sulla porta di Jarvis."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", PORT)) == 0
    finally:
        s.close()


def _serve():
    app.run(host="127.0.0.1", port=PORT, threaded=True, use_reloader=False)


def _open_ui(url):
    chrome_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in chrome_paths:
        if os.path.exists(path):
            # App-mode: niente barra degli indirizzi, sembra un'applicazione vera.
            subprocess.Popen([path, f"--app={url}", "--window-size=1480,940"])
            return
    webbrowser.open(url)


def main():
    # Se la porta e gia occupata, un'altra istanza di Jarvis sta girando (magari
    # un avvio precedente rimasto aperto). Aprire il browser adesso lo collegherebbe
    # a QUEL server, forse con codice vecchio: meglio fermarsi e dirlo chiaramente.
    if _port_in_use():
        print("=" * 60)
        print(f"  Jarvis e GIA in esecuzione sulla porta {PORT}.")
        print("  Chiudi l'altra finestra di Jarvis prima di riavviarlo,")
        print("  oppure apri direttamente http://127.0.0.1:%d/ nel browser." % PORT)
        print("  (Se sei sicuro che non giri nulla, un processo e rimasto appeso:")
        print("   aprilo dal Task Manager e chiudi i python.exe 'app.py', o riavvia il PC.)")
        print("=" * 60)
        input("\nPremi Invio per chiudere...")
        return

    threading.Thread(target=_serve, daemon=True).start()
    # Prepara in anticipo l'audio delle frasi fisse: i comandi diretti diventano
    # istantanei anche la prima volta. In un thread, non blocca l'avvio.
    threading.Thread(target=speech.prewarm, daemon=True).start()
    threading.Thread(target=_hotkey_loop, daemon=True).start()
    time.sleep(1.2)
    url = f"http://127.0.0.1:{PORT}/"
    try:
        _open_ui(url)
    except Exception:
        webbrowser.open(url)

    print("=" * 60)
    print(f"  J.A.R.V.I.S. attivo su {url}")
    if jarvis.has_key:
        print(f"  Cervello: Gemini ({jarvis.model}) - accesso completo al sistema")
    else:
        print("  Cervello: MODALITA LOCALE (nessuna API key)")
        print("  -> copia voice\\.env.example in voice\\.env e inserisci GEMINI_API_KEY")
        print("  -> la chiave si crea su https://aistudio.google.com/apikey")
    print("  Chiudi questa finestra per fermare Jarvis.")
    print("=" * 60)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nArresto di Jarvis.")


if __name__ == "__main__":
    main()
