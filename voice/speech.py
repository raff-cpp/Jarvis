"""Sintesi vocale di Jarvis con le voci neurali Microsoft (edge-tts).

Le voci del browser (speechSynthesis) su Chrome sono spesso femminili e
metalliche. Qui usiamo le stesse voci neurali di Edge, generate lato server:
qualita costante su qualunque browser, e due voci maschili italiane vere.

Se la sintesi fallisce (rete assente, servizio non raggiungibile) il server
risponde con un errore e la HUD ripiega sulla voce del browser.
"""
import asyncio
import os

try:
    import edge_tts
except ImportError:
    edge_tts = None

# Voci maschili italiane disponibili (verificate su edge-tts).
VOICES = {
    "diego": "it-IT-DiegoNeural",                       # timbro pieno, autorevole
    "giuseppe": "it-IT-GiuseppeMultilingualNeural",     # piu morbido, multilingue
    "elsa": "it-IT-ElsaNeural",                         # femminile
    "isabella": "it-IT-IsabellaNeural",                 # femminile
}

DEFAULT_VOICE = os.environ.get("JARVIS_VOICE", "diego").strip().lower()
# Cadenza da maggiordomo: leggermente piu lenta e piu grave del parlato neutro.
RATE = os.environ.get("JARVIS_VOICE_RATE", "-8%").strip()
PITCH = os.environ.get("JARVIS_VOICE_PITCH", "-12Hz").strip()

available = edge_tts is not None


def resolve(name):
    """Accetta sia 'diego' sia il nome completo 'it-IT-DiegoNeural'."""
    if not name:
        name = DEFAULT_VOICE
    key = str(name).strip().lower()
    if key in VOICES:
        return VOICES[key]
    if "-" in str(name):          # gia un nome completo
        return str(name).strip()
    return VOICES.get(DEFAULT_VOICE, "it-IT-DiegoNeural")


async def _synth(text, voice, rate, pitch):
    comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    audio = bytearray()
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
    return bytes(audio)


def synthesize(text, voice=None, rate=None, pitch=None):
    """Ritorna i byte MP3 del testo pronunciato. Solleva se la sintesi fallisce."""
    if not available:
        raise RuntimeError("edge-tts non installato")
    text = (text or "").strip()
    if not text:
        raise ValueError("testo vuoto")
    return asyncio.run(_synth(text, resolve(voice), rate or RATE, pitch or PITCH))
