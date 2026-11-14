"""Server di Jarvis: Flask + WebSocket, serve la HUD e lancia Chrome in app-mode.

Il server ascolta SOLO su 127.0.0.1: non e raggiungibile dalla rete locale.
La chiave di Google AI Studio resta in questo processo e non viene mai
inviata al browser.
"""
import atexit
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

import briefing                  # noqa: E402
import quota                     # noqa: E402
import speech                    # noqa: E402
import tools                     # noqa: E402
import watcher                   # noqa: E402
import brain                      # noqa: E402
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
    """Manda un messaggio a tutte le HUD collegate.

    Ritorna a quante e' arrivato: serve a capire se un annuncio e' stato
    davvero consegnato o se non c'era nessuna finestra aperta ad ascoltarlo.
    """
    data = json.dumps(obj)
    consegnati = 0
    with _clients_lock:
        morte = []
        for c in _clients:
            try:
                c.send(data)
                consegnati += 1
            except Exception:
                morte.append(c)
        for c in morte:
            _clients.discard(c)
    return consegnati


def _annuncia(testo):
    """Fa parlare Jarvis di sua iniziativa. Ritorna True se qualcuno ha sentito."""
    return _broadcast({"type": "announce", "text": testo}) > 0


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


@app.route("/api/models")
def models():
    """Stato dei modelli Gemini per il pannello HUD: quota residua per modello.

    Mostra tutta la catena (primario + ripieghi) con la quota giornaliera
    stimata rimasta, quale sta rispondendo ora e la sua latenza recente.
    Nessuna chiave, nessun dato sensibile: solo nomi di modello e numeri.
    """
    if not jarvis.has_key:
        return jsonify({"has_key": False, "active": None, "models": []})
    return jsonify({"has_key": True, "active": jarvis.model,
                    "models": quota.snapshot(jarvis.model_chain())})


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


@app.route("/api/announce", methods=["POST"])
def announce():
    """Fa pronunciare un avviso a Jarvis.

    Usato dai promemoria pianificati: cosi' li annuncia con la sua voce invece
    del riquadro anonimo di Windows. Se nessuna finestra e' aperta risponde
    delivered=false e il promemoria ripiega sulla notifica di sistema.

    Raggiungibile solo da questo PC: il server ascolta unicamente su 127.0.0.1.
    """
    data = request.get_json(silent=True) or {}
    testo = (data.get("text") or "").strip()
    if not testo:
        return jsonify({"error": "testo mancante"}), 400
    return jsonify({"delivered": _annuncia(testo[:400])})


@app.route("/api/pilota/stop", methods=["POST"])
def pilota_stop():
    """Freno del pilota automatico.

    La HUD lo tira quando dici "basta": la stessa parola che gia' zittisce
    Jarvis a meta' frase deve poter fermare anche le sue mani. Il freno viene
    controllato tra un passo e l'altro, e la mossa appena annunciata NON viene
    compiuta.
    """
    brain.FRENO_PILOTA.set()
    return jsonify({"status": "ok"})


@app.route("/wake", methods=["GET", "POST"])
def wake():
    """Richiamo esterno della scorciatoia globale Ctrl+Alt+J.

    La scorciatoia vive a livello di Windows (vedi scripts\\jarvis-hotkey.vbs
    e scripts\\installa-scorciatoia.ps1): cosi' funziona anche quando Jarvis
    e' spento, cosa che una scorciatoia registrata dentro il processo non
    potrebbe mai fare. Quando invece Jarvis e' gia' acceso, il launcher colpisce
    questo endpoint per portarlo in primo piano e aprire il microfono.

    Raggiungibile solo da 127.0.0.1: il server non ascolta sulla rete.
    """
    _on_hotkey()
    return jsonify({"status": "ok"})


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


def _campo(msg, nome):
    """Legge un campo di testo dal messaggio senza fidarsi del tipo.

    Un frame malformato (un numero o una lista dove ci va una stringa) faceva
    cadere l'intera connessione WebSocket, e con essa la HUD: il .strip()
    esplodeva e Flask chiudeva il socket. Qui tutto cio' che non e' testo
    diventa stringa vuota, cioe' "campo assente".
    """
    v = msg.get(nome)
    return v.strip() if isinstance(v, str) else ""


def _ws_loop(conn):
    while True:
        raw = conn.receive()
        if raw is None:
            break
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(msg, dict):
            continue

        def send(obj):
            try:
                conn.send(json.dumps(obj))
            except Exception:
                pass

        # Posizione della mano per l'overlay delle finestre reali: la giriamo
        # subito al processo overlay, senza passare da nulla d'altro.
        if msg.get("type") == "handpointer":
            _overlay_pointer(float(msg.get("x", 0.5)),
                             float(msg.get("y", 0.5)),
                             bool(msg.get("pinch")))
            continue

        if msg.get("type") == "holo_native":
            if msg.get("on"):
                _overlay_start()
            else:
                _overlay_stop()
            continue

        # I gesti non passano dal modello: sono azioni dirette e devono essere
        # immediate (il pinch aggiorna il volume molte volte al secondo).
        if msg.get("type") == "gesture":
            esito = _handle_gesture(msg)
            if esito:
                send({"type": "gesture_ack", "text": esito})
            continue

        if msg.get("type") == "presence":
            _handle_presence(_campo(msg, "event").lower())
            continue

        if msg.get("type") != "command":
            continue
        text = _campo(msg, "text")
        if not text:
            continue
        _segna_attivita()

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


_ultimo_log_scroll = 0.0
_ultima_finestra_scroll = None   # ultima finestra annunciata sulla HUD
_ultimo_errore_scroll = 0.0


def _log_gesto_scroll(value, esito):
    """Traccia in console, con parsimonia (~2 volte al secondo), che i gesti di
    scorrimento ARRIVANO al server e DOVE finiscono. Se non compare nulla il
    gesto non parte (webcam/posa); altrimenti la riga dice quale finestra sta
    scorrendo, o perche' non si e' potuto."""
    global _ultimo_log_scroll
    now = time.time()
    if now - _ultimo_log_scroll > 0.5:
        _ultimo_log_scroll = now
        dove = (esito.get("finestra") if esito.get("status") == "ok"
                else "NON RIUSCITO: " + esito.get("message", ""))
        print(f"[jarvis][gesto] scorrimento {value} -> {dove}")


def _riscontro_scroll(esito):
    """Testo per la HUD: solo quando cambia la finestra che scorre, o quando non
    si riesce a scorrere (al massimo ogni 5 s). A ogni evento sarebbe una
    valanga: il gesto ne manda fino a 30 al secondo."""
    global _ultima_finestra_scroll, _ultimo_errore_scroll
    if esito.get("status") != "ok":
        _ultima_finestra_scroll = None      # al prossimo successo, ridillo
        now = time.time()
        if now - _ultimo_errore_scroll > 5:
            _ultimo_errore_scroll = now
            return "Scorrimento non riuscito: " + esito.get("message", "")
        return None
    if esito.get("hwnd") and esito["hwnd"] != _ultima_finestra_scroll:
        _ultima_finestra_scroll = esito["hwnd"]
        return "Scorro: " + (esito.get("finestra") or "finestra in primo piano")
    return None


def _handle_gesture(msg):
    """Esegue un gesto. Ritorna un testo per la HUD, o None se silenzioso.

    Il risultato riportato e' quello VERO: prima questa funzione annunciava
    "Finestra spostata" anche quando lo strumento non aveva fatto nulla.
    """
    action = _campo(msg, "action").lower()
    value = msg.get("value")
    _segna_attivita()
    try:
        if action == "volume":
            tools.set_volume(level=int(value))
            return None                      # continuo: non intasare il registro

        if action == "scroll":
            # Prima l'esito veniva ignorato: se la rotellina finiva sulla
            # finestra sbagliata, o Windows la rifiutava, non lo sapeva nessuno.
            esito = tools.scroll_wheel(int(value))
            _log_gesto_scroll(value, esito)  # diagnostica parsimoniosa in console
            return _riscontro_scroll(esito)  # una riga sulla HUD, non una valanga

        if action == "media":
            verso = "next" if value == "next" else "previous"
            esito = tools.media_control(verso)
            if esito.get("status") != "ok":
                return "Comando musicale non riuscito"
            return "Traccia successiva" if verso == "next" else "Traccia precedente"

        if action == "window":
            # 'other' = l'altro schermo. Passiamo il valore cosi com'e: tradurlo
            # in 'left' faceva fallire lo spostamento sul monitor piu a sinistra.
            esito = tools.window_to_monitor(value if value in ("left", "right", "other") else "other")
            if esito.get("status") != "ok":
                return "Finestra non spostata: " + esito.get("message", "")
            return esito.get("message", "Finestra spostata")

        if action == "mute":
            tools.set_volume(mute=True)
            return "Audio commutato"
    except Exception as e:
        print("[jarvis] gesto non riuscito: " + redact(e), file=sys.stderr)
        return "Gesto non riuscito"
    return None


# ---- Overlay olografico delle finestre reali -------------------------------
# La modalita' "finestre reali" apre un overlay nativo trasparente
# (holo_overlay.py) che copre tutti i monitor e ti fa afferrare/spostare le
# finestre vere con la mano. Il tracciamento resta nel browser: la HUD ci manda
# la posizione della mano, noi la giriamo all'overlay via stdin. Per liberare il
# desktop, mentre l'overlay e' attivo la finestra della HUD viene minimizzata.

_overlay_proc = None
_overlay_lock = threading.Lock()
_OVERLAY_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "holo_overlay.py")


def _hud_window(ripristina):
    """Minimizza (False) o ripristina (True) la finestra Chrome della HUD.

    Filtra per classe 'Chrome_WidgetWin' cosi' non tocca la finestra della
    console, che ha lo stesso titolo 'J.A.R.V.I.S.'.
    """
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        target = []

        def cb(h, _l):
            if not user32.IsWindowVisible(h):
                return True
            t = ctypes.create_unicode_buffer(300)
            user32.GetWindowTextW(h, t, 300)
            c = ctypes.create_unicode_buffer(200)
            user32.GetClassNameW(h, c, 200)
            if "J.A.R.V.I.S." in t.value and c.value.startswith("Chrome_WidgetWin"):
                target.append(h)
                return False
            return True

        user32.EnumWindows(EnumProc(cb), 0)
        if target:
            user32.ShowWindow(target[0], 9 if ripristina else 6)  # SW_RESTORE / SW_MINIMIZE
    except Exception as e:
        print("[jarvis] finestra HUD non gestita: " + redact(e), file=sys.stderr)


def _overlay_start():
    global _overlay_proc
    with _overlay_lock:
        if _overlay_proc and _overlay_proc.poll() is None:
            return
        flags = 0x08000000 if os.name == "nt" else 0     # CREATE_NO_WINDOW
        try:
            _overlay_proc = subprocess.Popen(
                [sys.executable, _OVERLAY_PY],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                text=True, bufsize=1, creationflags=flags)
        except Exception as e:
            print("[jarvis] overlay non avviato: " + redact(e), file=sys.stderr)
            _overlay_proc = None
            return
        threading.Thread(target=_overlay_watch, args=(_overlay_proc,), daemon=True).start()
    _hud_window(ripristina=False)


def _overlay_pointer(x, y, pinch):
    with _overlay_lock:
        p = _overlay_proc
    if not p or p.poll() is not None:
        return
    try:
        p.stdin.write(json.dumps({"x": x, "y": y, "pinch": pinch}) + "\n")
        p.stdin.flush()
    except (OSError, ValueError):
        pass


def _overlay_stop():
    global _overlay_proc
    with _overlay_lock:
        p = _overlay_proc
        _overlay_proc = None
    if p and p.poll() is None:
        try:
            p.stdin.write('{"cmd":"stop"}\n')
            p.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            p.wait(timeout=2)
        except Exception:
            try:
                p.terminate()
            except Exception:
                pass
    _hud_window(ripristina=True)


def _overlay_watch(proc):
    """Se l'overlay si chiude da solo (ESC o «ESCI»), ripristina la HUD e la avvisa."""
    global _overlay_proc
    try:
        for line in proc.stdout:
            if "STOPPED" in line:
                break
    except Exception:
        pass
    with _overlay_lock:
        chiuso_da_solo = _overlay_proc is proc
        if chiuso_da_solo:
            _overlay_proc = None
    if chiuso_da_solo:
        _hud_window(ripristina=True)
        _broadcast({"type": "holo_native_stopped"})


# ---- Presenza --------------------------------------------------------------
# La webcam dei gesti sa gia' se sei davanti allo schermo. Sfruttarlo costa
# zero: quando ti siedi Jarvis ti accoglie, quando te ne vai mette in pausa la
# musica e blocca il PC.
#
# Il browser manda SOLO la parola "arrivo"/"uscita": nessuna immagine, nessun
# volto, nessuna identita' lasciano mai la pagina.
#
# Il blocco automatico e' l'azione piu' invasiva di tutto il progetto, quindi e'
# protetta tre volte: il volto deve mancare per un minuto e mezzo, non devi aver
# usato Jarvis di recente, e un preavviso parlato ti da' il tempo di rientrare.

PRESENZA = os.environ.get("JARVIS_PRESENZA", "on").strip().lower() != "off"
PRESENZA_BLOCCO = os.environ.get("JARVIS_PRESENZA_BLOCCO", "on").strip().lower() != "off"

# Il browser manda "arrivo" solo dopo un'assenza vera, quindi qui resta da
# assorbire i soli doppioni: due HUD aperte, o l'interruttore GESTI spento e
# riacceso. DEVE restare ben sotto la soglia del browser, altrimenti torna a
# succedere quello che succedeva prima: ti alzi, torni, e lui non ti saluta
# perche' il server sta ancora zittendo il saluto precedente.
RIENTRO_SILENZIO_S = 30
ATTIVITA_RECENTE_S = 60     # se hai appena parlato a Jarvis, non e' un'assenza
PRESENZA_DEBOUNCE_S = 5     # piu' finestre aperte mandano lo stesso evento

_presenza_lock = threading.Lock()
_ultima_attivita = 0.0
_ultimo_saluto = 0.0
_ultimo_evento = {}


def _segna_attivita():
    """Registra che stai usando Jarvis: vale come 'sono qui'."""
    global _ultima_attivita
    _ultima_attivita = time.time()


def _handle_presence(evento):
    if not PRESENZA or evento not in ("arrivo", "uscita", "preavviso", "annulla"):
        return
    adesso = time.time()
    with _presenza_lock:
        # Due finestre della HUD manderebbero lo stesso evento in contemporanea.
        if adesso - _ultimo_evento.get(evento, 0) < PRESENZA_DEBOUNCE_S:
            return
        _ultimo_evento[evento] = adesso
    # Le azioni parlano e chiamano PowerShell: fuori dal thread del WebSocket.
    threading.Thread(target=_presenza_azione, args=(evento, adesso),
                     daemon=True).start()


def _presenza_azione(evento, adesso):
    global _ultimo_saluto
    try:
        if evento == "arrivo":
            _segna_attivita()
            # Il briefing del giorno si fa una volta sola: se non e' ancora
            # stato fatto questo e' il momento giusto, sei appena arrivato.
            if BRIEFING_AUTO and _fai_briefing_se_serve():
                _ultimo_saluto = adesso
                return
            if adesso - _ultimo_saluto < RIENTRO_SILENZIO_S:
                return          # sei uscito un attimo: non serve un discorso
            if _annuncia("Bentornato, signore."):
                _ultimo_saluto = adesso
            return

        if evento == "preavviso":
            if not PRESENZA_BLOCCO or _in_uso(adesso):
                return
            _annuncia("Non la vedo piu, signore. Blocco la postazione fra quindici secondi.")
            return

        if evento == "uscita":
            if not PRESENZA_BLOCCO or _in_uso(adesso):
                return
            # Prima la musica, poi il blocco: dopo il lock la sessione resta
            # attiva e la traccia continuerebbe a suonare a schermo spento.
            tools.pause_if_playing()
            tools.power_control("lock")
            return

        # "annulla": sei rientrato prima del blocco. Niente voce: il preavviso
        # era gia' un'interruzione, raddoppiarla darebbe fastidio e basta.
    except Exception as e:
        print("[jarvis] presenza: " + redact(e), file=sys.stderr)


def _in_uso(adesso):
    """True se stai chiaramente usando Jarvis: allora non e' un'assenza."""
    return adesso - _ultima_attivita < ATTIVITA_RECENTE_S


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
        # Fallisce anche quando la scorciatoia di Windows sul desktop possiede
        # gia' la combinazione (vedi scripts\installa-scorciatoia.ps1): in quel
        # caso e' quella a gestirla, sia a Jarvis acceso (endpoint /wake) sia da
        # spento. Nessun problema: e' anzi la configurazione consigliata.
        print(f"  Scorciatoia {HOTKEY.upper()}: gestita fuori dal processo "
              "(scorciatoia di Windows sul desktop o altro programma).",
              file=sys.stderr)
        return
    print(f"  Scorciatoia globale (solo a Jarvis acceso): {HOTKEY.upper()}")
    msg = wintypes.MSG()
    WM_HOTKEY = 0x0312
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY:
            _on_hotkey()


# ---- Iniziativa di Jarvis --------------------------------------------------

PROATTIVO = os.environ.get("JARVIS_PROATTIVO", "on").strip().lower() != "off"
BRIEFING_AUTO = os.environ.get("JARVIS_BRIEFING_AUTO", "on").strip().lower() != "off"
CITTA = os.environ.get("JARVIS_CITTA", "").strip()

_watcher = watcher.Watcher(_annuncia)

# Il pilota automatico racconta ogni mossa PRIMA di compierla: e' cosi' che
# puoi accorgerti in tempo che sta sbagliando e fermarlo con "basta".
brain.NARRATORE = _annuncia

# Due strade portano al briefing (l'avvio del server e il tuo arrivo davanti
# alla webcam): il lucchetto impedisce che partano insieme e lo dicano due volte.
_briefing_lock = threading.Lock()


def _fai_briefing_se_serve():
    """Annuncia il riepilogo se non e' ancora stato fatto oggi. True se detto."""
    with _briefing_lock:
        if briefing.gia_fatto_oggi():
            return False
        try:
            testo = briefing.componi(CITTA)
        except Exception as e:
            print("[jarvis] briefing non riuscito: " + redact(e), file=sys.stderr)
            return False
        if not _annuncia(testo):
            return False
        briefing.segna_fatto()
        return True


def _briefing_di_avvio():
    """Al primo avvio della giornata, Jarvis fa il punto della situazione.

    Aspetta che una finestra sia collegata: annunciarlo nel vuoto lo
    sprecherebbe. Se dopo un minuto non si collega nessuno, rinuncia.
    """
    if briefing.gia_fatto_oggi():
        return
    for _ in range(60):
        time.sleep(1)
        with _clients_lock:
            if not _clients:
                continue
        _fai_briefing_se_serve()
        return


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

    # Se il server muore con l'overlay aperto, non lasciare la HUD minimizzata.
    atexit.register(_overlay_stop)

    threading.Thread(target=_serve, daemon=True).start()
    # Prepara in anticipo l'audio delle frasi fisse: i comandi diretti diventano
    # istantanei anche la prima volta. In un thread, non blocca l'avvio.
    threading.Thread(target=speech.prewarm, daemon=True).start()
    threading.Thread(target=_hotkey_loop, daemon=True).start()
    # Sonda a costo minimo del modello primario: cosi il pannello quota sulla HUD
    # e' realistico gia all'avvio (mostra subito se il primario e' esaurito),
    # senza aspettare il primo comando. Spende al piu una richiesta al giorno.
    threading.Thread(target=jarvis.sonda_quota, daemon=True).start()
    if PROATTIVO:
        _watcher.start()
    if BRIEFING_AUTO:
        threading.Thread(target=_briefing_di_avvio, daemon=True).start()
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
    if PROATTIVO:
        print("  Iniziativa: attiva (download completati, batteria scarica)")
    print("  Chiudi questa finestra per fermare Jarvis.")
    print("=" * 60)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nArresto di Jarvis.")


if __name__ == "__main__":
    main()
