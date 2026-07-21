"""Elenca i modelli Gemini disponibili sulla tua chiave di Google AI Studio.

Uso:  python modelli.py

Serve a scegliere il valore giusto per JARVIS_MODEL nel file .env: i nomi dei
modelli cambiano nel tempo, questo li chiede direttamente a Google.
"""
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

from google import genai

key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
if not key:
    sys.exit("Nessuna chiave trovata. Metti GEMINI_API_KEY nel file voice/.env")

client = genai.Client(api_key=key)

print("Modelli utilizzabili per la generazione di contenuti:\n")
trovati = 0
for m in client.models.list():
    actions = getattr(m, "supported_actions", None) or []
    if actions and "generateContent" not in actions:
        continue
    name = (m.name or "").removeprefix("models/")
    print(f"  {name:<45} {m.display_name or ''}")
    trovati += 1

print(f"\n{trovati} modelli disponibili.")
print("Copia il nome scelto in JARVIS_MODEL dentro voice/.env")
print("Per un assistente vocale conviene un modello 'flash': risponde prima.")
