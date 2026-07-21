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

from flask import Flask, jsonify, send_from_directory
from flask_sock import Sock

# Carica la .env (GEMINI_API_KEY, JARVIS_MODEL, ...) prima di importare il brain.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

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


@app.after_request
def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/")
def index():
    return send_from_directory(WEB, "index.html")


@app.route("/api/info")
def info():
    # Espone solo un booleano e il nome del modello: MAI la chiave.
    return jsonify({"has_key": jarvis.has_key, "model": jarvis.model})


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
    while True:
        raw = conn.receive()
        if raw is None:
            break
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            continue

        if msg.get("type") != "command":
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue

        def send(obj):
            try:
                conn.send(json.dumps(obj))
            except Exception:
                pass

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
            send({"type": "say", "text": "Si e verificato un errore interno, Raffaele."})
            # redact(): la chiave non deve finire nemmeno nei log di console.
            print("[jarvis] errore nel turno: " + redact(e), file=sys.stderr)
        send({"type": "done"})


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
    threading.Thread(target=_serve, daemon=True).start()
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
