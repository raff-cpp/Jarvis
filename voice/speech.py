"""Sintesi vocale di Jarvis con le voci neurali Microsoft (edge-tts).

Le voci del browser (speechSynthesis) su Chrome sono spesso femminili e
metalliche. Qui usiamo le stesse voci neurali di Edge, generate lato server:
qualita costante su qualunque browser, e due voci maschili italiane vere.

Se la sintesi fallisce (rete assente, servizio non raggiungibile) il server
risponde con un errore e la HUD ripiega sulla voce del browser.
"""
import asyncio
import os
import threading
import time

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


"""Cache dell'audio già sintetizzato.

I comandi diretti rispondono in pochi millisecondi ma dicono sempre le stesse
frasi ("Fatto.", "Volume alzato."): sintetizzarle daccapo ogni volta aggiungeva
quasi un secondo di silenzio a un'azione per il resto istantanea. Qui l'audio
viene tenuto in memoria e riusato.
"""
_cache = {}
_cache_lock = threading.Lock()
_CACHE_MAX = 200

# Frasi fisse pronunciate dai comandi diretti e dalla HUD: le prepariamo
# all'avvio, così sono pronte già alla prima volta.
FRASI_FISSE = [
    "Fatto.",
    "Volume alzato.",
    "Volume abbassato.",
    "Traccia successiva.",
    "Traccia precedente.",
    "Blocco lo schermo.",
    "Sono a sua disposizione, signore.",
    "Certamente.",
    "Non sono riuscito a completare la richiesta. Controlla la console per il dettaglio.",
    # Identica a brain.MSG_RETE. Va preparata ADESSO, a rete funzionante: quando
    # servira' la rete sara' giu' e non si potrebbe piu' sintetizzarla.
    "Non riesco a raggiungere i server di Google: la connessione o il DNS "
    "non rispondono. Riprova tra qualche secondo.",
]


def _chiave(text, voice, rate, pitch):
    return (text, resolve(voice), rate or RATE, pitch or PITCH)


def synthesize(text, voice=None, rate=None, pitch=None):
    """Ritorna i byte MP3 del testo pronunciato. Solleva se la sintesi fallisce."""
    if not available:
        raise RuntimeError("edge-tts non installato")
    text = (text or "").strip()
    if not text:
        raise ValueError("testo vuoto")

    k = _chiave(text, voice, rate, pitch)
    with _cache_lock:
        pronto = _cache.get(k)
    if pronto is not None:
        return pronto

    # Un secondo tentativo se a fallire e' la RETE (OSError: comprende il DNS che
    # non risolve, getaddrinfo failed). Su questa rete il DNS del router inciampa
    # ogni tanto, e un singolo inciampo faceva parlare Jarvis con la voce del
    # browser. Un rifiuto del servizio (es. 403) non e' di rete: non si ritenta.
    for attesa in (0.5, None):
        try:
            audio = asyncio.run(_synth(text, k[1], k[2], k[3]))
            break
        except OSError as e:
            # TimeoutError e' un OSError, ma arriva dopo molti secondi:
            # ritentarlo raddoppierebbe un'attesa gia' lunga.
            if attesa is None or isinstance(e, TimeoutError):
                raise
            time.sleep(attesa)
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:          # sfoltisce le più vecchie
            for vecchia in list(_cache)[:_CACHE_MAX // 4]:
                _cache.pop(vecchia, None)
        _cache[k] = audio
    return audio


def prewarm(voice=None):
    """Sintetizza in anticipo le frasi fisse. Va lanciato in un thread a parte:
    ognuna costa circa un secondo e non deve rallentare l'avvio."""
    if not available:
        return 0
    fatte = 0
    for frase in FRASI_FISSE:
        try:
            synthesize(frase, voice)
            fatte += 1
        except Exception:
            break          # niente rete: riproveremo alla prima richiesta vera
    return fatte


def cache_info():
    with _cache_lock:
        return {"voci": len(_cache),
                "byte": sum(len(v) for v in _cache.values())}
