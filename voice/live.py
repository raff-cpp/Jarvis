"""Conversazione dal vivo: Jarvis ascolta e risponde a voce, in tempo reale.

Il cervello classico (brain.py) lavora a turni: il browser trascrive la frase,
il modello risponde con un testo, la sintesi vocale lo legge. Ogni passaggio
aggiunge attesa, e la voce resta quella sintetica di edge-tts.

Qui il microfono va dritto a gemini-3.8-live, che ascolta e PARLA da solo: la
risposta comincia in circa un secondo, la voce e' naturale, e lo si puo'
interrompere a meta' frase come una persona. Gli strumenti sono gli stessi del
cervello classico (Jarvis.esegui_strumento), conferme comprese: il modello li
chiama, li eseguiamo qui, il risultato torna nella stessa conversazione.

Percorso dell'audio:
    microfono -> HUD (PCM 16 kHz) -> /ws -> qui -> Gemini Live
    Gemini Live -> qui (PCM 24 kHz) -> /ws -> HUD -> casse
La chiave resta in questo processo: il browser parla solo con il nostro
server, mai con Google.

L'SDK Live e' asincrono, il server Flask no. Tutte le conversazioni girano su
un unico ciclo asyncio in un thread dedicato; il resto del server le comanda
con poche funzioni thread-safe in fondo al file (avvia, audio, testo, ferma,
annuncia).

Limiti del servizio e come li gestiamo:
  - un collegamento dura circa 10 minuti: Google avvisa (GoAway) e noi ci
    ricolleghiamo con la maniglia di ripresa, senza perdere la conversazione;
  - senza compressione una sessione solo audio si ferma a 15 minuti: la
    compressione a finestra scorrevole toglie il limite;
  - le maniglie di ripresa valgono 2 ore: le riusiamo anche per la
    conversazione successiva, se arriva entro JARVIS_LIVE_MEMORIA_MIN minuti,
    cosi' Jarvis ricorda cosa vi siete detti ("e adesso alzalo ancora").
"""
import asyncio
import datetime
import json
import os
import threading
import time

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

import brain


def _flag(nome, predefinito="on"):
    return os.environ.get(nome, predefinito).strip().lower() != "off"


def _intero(nome, predefinito, minimo, massimo):
    try:
        v = int(os.environ.get(nome, "").strip() or predefinito)
    except ValueError:
        v = predefinito
    return max(minimo, min(massimo, v))


ATTIVA = _flag("JARVIS_LIVE")
MODELLO = os.environ.get("JARVIS_LIVE_MODEL", "").strip() or "gemini-3.8-live"
VOCE = os.environ.get("JARVIS_LIVE_VOICE", "").strip() or "Charon"
# Secondi di silenzio dopo cui la HUD chiude la conversazione e torna ad
# aspettare la parola d'attivazione. Il conto lo tiene la HUD, che sa quando
# l'audio ha davvero finito di suonare; qui c'e' solo una rete di sicurezza.
SILENZIO_S = _intero("JARVIS_LIVE_SILENZIO_S", 20, 5, 600)
# Entro quanti minuti una nuova conversazione riprende la precedente.
# Le maniglie di Google valgono 2 ore: oltre non si puo' andare.
MEMORIA_MIN = _intero("JARVIS_LIVE_MEMORIA_MIN", 30, 0, 115)
# off = mentre Jarvis parla la HUD non gli manda il microfono: niente
# interruzioni a voce, ma nemmeno il rischio che senta la propria eco dalle
# casse se l'eliminazione dell'eco del browser non basta.
INTERRUZIONI = _flag("JARVIS_LIVE_INTERRUZIONI")

# Le 30 voci di Gemini con il carattere che Google indica per ciascuna. Le
# prime tre sono quelle dei campioni ascoltati: pacate, da maggiordomo.
VOCI = {
    "Charon": "informativa",
    "Algieba": "morbida",
    "Sadaltager": "competente",
    "Achernar": "dolce",
    "Achird": "amichevole",
    "Algenib": "roca",
    "Alnilam": "decisa",
    "Aoede": "ariosa",
    "Autonoe": "luminosa",
    "Callirrhoe": "rilassata",
    "Despina": "morbida",
    "Enceladus": "sussurrata",
    "Erinome": "chiara",
    "Fenrir": "energica",
    "Gacrux": "matura",
    "Iapetus": "chiara",
    "Kore": "decisa",
    "Laomedeia": "allegra",
    "Leda": "giovanile",
    "Orus": "decisa",
    "Puck": "allegra",
    "Pulcherrima": "diretta",
    "Rasalgethi": "informativa",
    "Sadachbia": "vivace",
    "Schedar": "equilibrata",
    "Sulafat": "calda",
    "Umbriel": "rilassata",
    "Vindemiatrix": "gentile",
    "Zephyr": "luminosa",
    "Zubenelgenubi": "informale",
}

# Strumenti che esistono solo dal vivo.
_SPECS_LIVE = [
    brain._spec("get_datetime",
                "Data e ora attuali. Usalo ogni volta che ti servono: quelle scritte "
                "nelle istruzioni risalgono all'inizio della conversazione."),
    brain._spec("end_conversation",
                "Chiude la conversazione dal vivo. Chiamalo quando il signore ti congeda "
                "(\"grazie, e' tutto\", \"a posto cosi'\", \"puoi andare\"), dopo averlo "
                "salutato in poche parole."),
]
_ETICHETTE = {"get_datetime": "Controllo l'ora", "end_conversation": "Chiudo la conversazione"}

_ISTRUZIONI_LIVE = """

CONVERSAZIONE DAL VIVO:
- Stai parlando a voce con il signore, in tempo reale, come al telefono. Rispondi sempre e solo in italiano.
- Il signore puo' interromperti: se lo fa, fermati e ascolta, senza riprendere la frase interrotta.
- Se ti interrompe con "basta", "stop", "fermati" o "zitto", smetti di parlare e aspetta in silenzio: e' un'interruzione, non un congedo, quindi NON chiudere la conversazione.
- Se senti voci di sottofondo, una televisione o persone che non si rivolgono a te, resta in silenzio.
- Quando il signore ti congeda ("grazie, e' tutto", "a posto cosi'", "puoi andare"), salutalo in poche parole e chiama end_conversation.
- La data e l'ora scritte qui sotto sono quelle di inizio conversazione: quando ti servono, chiama get_datetime.
- I messaggi che cominciano con [AVVISO DI SISTEMA] non li dice il signore: sono notifiche automatiche del PC (download, batteria, promemoria). Riferiscile con una frase breve, con parole tue, senza aggiungere altro.
- Prima di uno strumento lento (pilota automatico, ricerca web, lettura di una pagina) puoi dire due parole, per esempio "Un attimo.", e poi agire: e' l'unica eccezione alla regola di non annunciare cosa stai per fare."""

# Frasi per la HUD quando la conversazione non parte o cade. Brevi: la HUD le
# pronuncia con la voce classica.
_MESSAGGI = {
    "quota": "La conversazione dal vivo ha esaurito la quota gratuita. "
             "Per ora torno alla modalità classica.",
    "modello": f"Il modello {MODELLO} non risponde con questa chiave. "
               "Torno alla modalità classica.",
    "rete": "Non riesco a raggiungere Google per la conversazione dal vivo. "
            "Controlla la connessione.",
    "altro": "La conversazione dal vivo si è interrotta per un errore. "
             "Il dettaglio è in console.",
}

_MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
         "agosto", "settembre", "ottobre", "novembre", "dicembre"]

# Audio del microfono piu' vecchio di cosi' non va piu' mandato: si e'
# accumulato mentre ci si collegava, e risentirlo in ritardo confonderebbe il
# rilevamento del parlato.
_AUDIO_SCADUTO_S = 3.0


# ---- collegamenti con il resto del server -----------------------------------

_ON_ATTIVITA = lambda: None          # noqa: E731  - il signore sta usando Jarvis
_RIPIEGO_ANNUNCIO = lambda t: False  # noqa: E731  - avviso detto dalla voce classica


def configura(on_attivita=None, ripiego_annuncio=None):
    """Il server collega qui la presenza e la voce classica degli avvisi."""
    global _ON_ATTIVITA, _RIPIEGO_ANNUNCIO
    if on_attivita:
        _ON_ATTIVITA = on_attivita
    if ripiego_annuncio:
        _RIPIEGO_ANNUNCIO = ripiego_annuncio


def disponibile(jarvis):
    return ATTIVA and types is not None and bool(getattr(jarvis, "has_key", False))


def voce_valida(nome):
    """Una voce dell'elenco, mai un nome arbitrario arrivato dal browser."""
    cercata = str(nome or "").strip().lower()
    for v in VOCI:
        if v.lower() == cercata:
            return v
    return VOCE


def info():
    """Per /api/info: cosa serve alla HUD per comportarsi bene."""
    return {"live": ATTIVA, "live_model": MODELLO, "live_voice": voce_valida(VOCE),
            "live_silenzio_s": SILENZIO_S, "live_interruzioni": INTERRUZIONI}


def elenco_voci():
    return [{"id": v, "label": f"{v} — {d}"} for v, d in VOCI.items()]


# ---- infrastruttura: client, ciclo asyncio, memoria di ripresa ---------------

_client = None
_client_lock = threading.Lock()


def _cliente():
    """Client dedicato: la stessa chiave del cervello, ma API v1beta (la Live)."""
    global _client
    with _client_lock:
        if _client is None:
            _client = genai.Client(api_key=brain._api_key(),
                                   http_options={"api_version": "v1beta"})
        return _client


_ciclo = None
_ciclo_lock = threading.Lock()


def _loop():
    global _ciclo
    with _ciclo_lock:
        if _ciclo is None:
            ciclo = asyncio.new_event_loop()
            threading.Thread(target=ciclo.run_forever, name="jarvis-live",
                             daemon=True).start()
            _ciclo = ciclo
        return _ciclo


_ripresa = {"handle": None, "t": 0.0, "voce": None, "istruzioni": None}
_ripresa_lock = threading.Lock()


def _ricorda(handle, voce, istruzioni):
    with _ripresa_lock:
        _ripresa.update(handle=handle, t=time.time(), voce=voce, istruzioni=istruzioni)


def _da_riprendere(voce):
    """Maniglia e istruzioni della conversazione precedente, se ancora valide.

    Le istruzioni tornano con la maniglia: la sessione ripresa e' la stessa,
    e rimandarle diverse (l'ora cambia a ogni minuto) non avrebbe senso.
    Una voce diversa invece vuole una sessione nuova.
    """
    with _ripresa_lock:
        r = dict(_ripresa)
    if (r["handle"] and r["voce"] == voce and MEMORIA_MIN > 0
            and time.time() - r["t"] < MEMORIA_MIN * 60):
        return r["handle"], r["istruzioni"]
    return None, None


def dimentica():
    """La prossima conversazione riparte da zero (pulsante RESET)."""
    with _ripresa_lock:
        _ripresa.update(handle=None, t=0.0, voce=None, istruzioni=None)


_dichiarazioni_cache = None


def _dichiarazioni():
    global _dichiarazioni_cache
    if _dichiarazioni_cache is None:
        _dichiarazioni_cache = brain._declarations() + [
            types.FunctionDeclaration(name=s["name"], description=s["description"])
            for s in _SPECS_LIVE]
    return _dichiarazioni_cache


def _config(voce, handle, istruzioni):
    vad = types.AutomaticActivityDetection(
        # Bassa: serve una voce vera, non un rumore o un residuo d'eco, per
        # decidere che il signore ha cominciato a parlare (e interrompere).
        start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
        # Poco sotto gli 800 ms predefiniti: risposta piu' pronta, ma non cosi'
        # stretta da tagliare una frase alla prima pausa per respirare.
        silence_duration_ms=700,
    )
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voce))),
        system_instruction=istruzioni,
        tools=[types.Tool(function_declarations=_dichiarazioni())],
        input_audio_transcription=types.AudioTranscriptionConfig(
            language_hints=types.LanguageHints(language_codes=["it-IT"]),
            custom_vocabulary=["Jarvis"]),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(automatic_activity_detection=vad),
        session_resumption=types.SessionResumptionConfig(handle=handle),
        context_window_compression=types.ContextWindowCompressionConfig(
            trigger_tokens=32000, sliding_window=types.SlidingWindow(target_tokens=16000)),
    )


def _data_ora():
    adesso = datetime.datetime.now()
    return {"status": "ok",
            "data": f"{brain._GIORNI[adesso.weekday()]} {adesso.day} "
                    f"{_MESI[adesso.month - 1]} {adesso.year}",
            "ora": adesso.strftime("%H:%M")}


def _secondi(durata, predefinito):
    """'49.5s' (o un numero) -> secondi. Il formato preciso dipende dall'SDK."""
    try:
        if isinstance(durata, (int, float)):
            return float(durata)
        if hasattr(durata, "total_seconds"):
            return float(durata.total_seconds())
        return float(str(durata).strip().rstrip("s"))
    except (TypeError, ValueError):
        return predefinito


def _tipo_errore(e):
    """quota | modello | rete | altro: decide il messaggio e se riprovare."""
    s = str(e)
    basso = s.lower()
    if "resource_exhausted" in basso or "quota" in basso or "429" in s or "rate limit" in basso:
        return "quota"
    if ("not found" in basso or "not supported" in basso or "unsupported" in basso
            or " 404" in s):
        return "modello"
    if brain._is_network(e) or isinstance(e, (OSError, asyncio.TimeoutError)):
        return "rete"
    if "1006" in s or "abnormal" in basso or "1001" in s or "going away" in basso:
        return "rete"
    return "altro"


class _Fine(Exception):
    """La conversazione deve finire (chiesto dalla HUD, o silenzio prolungato)."""


class _Ricollega(Exception):
    """Google ha annunciato la chiusura del collegamento: ci si ricollega."""


# ---- la conversazione ---------------------------------------------------------

class Conversazione:
    """Una conversazione dal vivo legata a una finestra della HUD.

    I metodi pubblici sono chiamabili da qualunque thread; tutto il resto gira
    nel ciclo asyncio dedicato e tocca lo stato solo da li'.
    """

    def __init__(self, jarvis, canale, voce, al_termine, sid=0):
        self.jarvis = jarvis
        self.canale = canale
        self.voce = voce
        self.al_termine = al_termine
        # Numero scelto dalla HUD: accompagna ogni messaggio, cosi' la HUD
        # scarta quelli in ritardo di una conversazione gia' chiusa.
        self.sid = sid
        self.loop = _loop()
        self.coda = asyncio.Queue()
        self.chiusa = False
        self.aperta = False          # collegata a Google almeno una volta
        self.motivo = ""
        self._sessione = None
        self._handle = None
        self._istruzioni = None
        self._strumenti = {}         # id chiamata -> nome, in esecuzione
        self._annullati = set()
        self._congedo = None         # None | "chiesto" | "risposto"
        self._genera = False         # il modello sta producendo un turno
        self._genera_dal = 0.0
        self._fine_audio = 0.0       # quando la HUD finira' di suonare (stima)
        self._ultima_voce = 0.0      # ultima trascrizione del signore
        self._ultima_attivita = time.monotonic()
        self._annunci = []
        self._muto = False           # pilota in azione: il microfono non va a Google
        self._go_away = False
        self._go_away_entro = 0.0    # quando il collegamento verra' chiuso da Google
        self._ultimo_turno = 0.0     # fine dell'ultimo turno
        self._handle_t = 0.0         # arrivo dell'ultima maniglia di ripresa
        self._token = 0
        self._t0 = time.monotonic()
        # asyncio tiene solo riferimenti deboli ai task: senza questi, uno
        # strumento in corso potrebbe sparire a meta' esecuzione.
        self._compiti = set()
        self._futuro = None

    def _lancia(self, coro):
        t = asyncio.ensure_future(coro)
        self._compiti.add(t)
        t.add_done_callback(self._compiti.discard)
        return t

    # ---- comandi, da qualunque thread ---------------------------------------
    def avvia(self, testo=None):
        self._futuro = asyncio.run_coroutine_threadsafe(self._vita(testo), self.loop)

    def _metti(self, voce):
        if self.chiusa:
            return
        try:
            self.loop.call_soon_threadsafe(self.coda.put_nowait, voce)
        except RuntimeError:
            pass                     # ciclo fermo: il server si sta spegnendo

    def audio(self, pcm):
        self._metti(("audio", pcm, time.monotonic()))

    def testo(self, testo):
        self._metti(("testo", testo, 0))

    def annuncia(self, testo):
        self._metti(("annuncio", testo, 0))

    def ferma(self, motivo):
        if self.chiusa:
            return
        self.motivo = self.motivo or motivo
        self.chiusa = True
        try:
            self.loop.call_soon_threadsafe(self.coda.put_nowait, ("fine", motivo, 0))
        except RuntimeError:
            pass

    # ---- ciclo di vita ----------------------------------------------------------
    async def _vita(self, testo):
        if testo:
            self.coda.put_nowait(("testo", testo, 0))
        self._hud({"type": "live_state", "state": "connecting"})
        handle, istruzioni = _da_riprendere(self.voce)
        self._handle = handle
        self._istruzioni = istruzioni or self._componi_istruzioni()
        attese_rete = list(brain._ATTESE_RETE)
        attese_ripresa = [0.8, 1.5, 2.5]
        cadute = 0
        try:
            while not self.chiusa:
                inizio = time.monotonic()
                try:
                    await self._collegati(handle)
                except _Fine:
                    return
                except _Ricollega:
                    handle = self._handle
                    print("[jarvis][live] Google rinnova il collegamento: riprendo la sessione.")
                    continue
                except Exception as e:
                    if self.chiusa:
                        return
                    tipo = _tipo_errore(e)
                    # Riaprendo subito dopo una chiusura, Google puo' non aver
                    # ancora liberato la sessione precedente ("already connected
                    # to an existing client", visto nelle prove): e' questione
                    # di un attimo, e ripartire da zero perderebbe la memoria.
                    if (handle and not self.aperta and attese_ripresa
                            and "already connected" in str(e).lower()):
                        await asyncio.sleep(attese_ripresa.pop(0))
                        continue
                    # La maniglia di una conversazione precedente puo' essere
                    # scaduta o rifiutata: si riparte da zero, una volta sola.
                    if handle and not self.aperta and tipo != "rete":
                        print("[jarvis][live] ripresa rifiutata (%s): sessione nuova."
                              % brain.redact(e))
                        dimentica()
                        handle = self._handle = None
                        self._istruzioni = self._componi_istruzioni()
                        continue
                    # Caduta a conversazione avviata: si riprende dove eravamo.
                    if self.aperta and self._handle and tipo in ("rete", "altro"):
                        if time.monotonic() - inizio > 30:
                            cadute = 0
                        cadute += 1
                        if cadute <= 3:
                            print("[jarvis][live] collegamento caduto (%s): riprendo, "
                                  "tentativo %d." % (brain.redact(e), cadute))
                            handle = self._handle
                            await asyncio.sleep(0.5 * cadute)
                            continue
                    # Rete che inciampa mentre ci si collega: come nel cervello
                    # classico, qualche tentativo ravvicinato prima di arrendersi.
                    if tipo == "rete" and not self.aperta and attese_rete:
                        attesa = attese_rete.pop(0)
                        print("[jarvis][live] Google non raggiungibile (%s): riprovo fra %.1fs."
                              % (brain.redact(e), attesa))
                        await asyncio.sleep(attesa)
                        continue
                    self._errore(tipo, e)
                    return
        finally:
            self._concludi()

    async def _collegati(self, handle):
        cfg = _config(self.voce, handle, self._istruzioni)
        async with _cliente().aio.live.connect(model=MODELLO, config=cfg) as s:
            self._sessione = s
            self._go_away = False
            self._go_away_entro = 0.0
            self._genera = False
            if not self.aperta:
                self.aperta = True
                print(f"[jarvis][live] conversazione aperta ({MODELLO}, voce {self.voce}"
                      + (", ripresa la precedente" if handle else "") + ").")
                self._hud({"type": "live_state", "state": "open",
                           "resumed": bool(handle), "voice": self.voce})
            compiti = [asyncio.ensure_future(c) for c in
                       (self._ricevi(s), self._invia(s), self._orologio(s))]
            try:
                fatti, _ = await asyncio.wait(compiti, return_when=asyncio.FIRST_COMPLETED)
            finally:
                self._sessione = None
                for c in compiti:
                    c.cancel()
                await asyncio.gather(*compiti, return_exceptions=True)
            errori = [c.exception() for c in fatti
                      if not c.cancelled() and c.exception() is not None]
            # Se insieme alla chiusura voluta e' arrivato anche l'errore del
            # socket che si chiude, conta la chiusura voluta.
            for tipo in (_Fine, _Ricollega):
                for e in errori:
                    if isinstance(e, tipo):
                        raise e
            if errori:
                raise errori[0]
            raise _Fine()

    def _concludi(self):
        self.chiusa = True
        self._sessione = None
        # Il pilota non deve continuare a cliccare per una conversazione finita.
        if "use_computer" in self._strumenti.values():
            brain.FRENO_PILOTA.set()
        if self._handle:
            _ricorda(self._handle, self.voce, self._istruzioni)
        # Gli avvisi non ancora detti escono con la voce classica: un
        # promemoria non deve sparire perche' la conversazione e' finita.
        while not self.coda.empty():
            tipo, dato, _ = self.coda.get_nowait()
            if tipo == "annuncio":
                self._annunci.append(dato)
        for testo in self._annunci:
            try:
                _RIPIEGO_ANNUNCIO(testo)
            except Exception:
                pass
        self._annunci = []
        durata = time.monotonic() - self._t0
        print(f"[jarvis][live] conversazione chiusa ({self.motivo or 'fine'}): "
              f"{durata:.0f}s, circa {self._token} token.")
        self._hud({"type": "live_state", "state": "closed", "reason": self.motivo or "fine"})
        try:
            self.al_termine(self)
        except Exception:
            pass

    def _errore(self, tipo, e):
        self.motivo = "errore"
        print(f"[jarvis][live] errore ({tipo}): {brain.redact(e)}")
        self._hud({"type": "live_state", "state": "error", "kind": tipo,
                   "message": _MESSAGGI[tipo]})

    def _componi_istruzioni(self):
        return (brain.SYSTEM + _ISTRUZIONI_LIVE + "\n" + brain._NO_SEARCH_NOTE
                + "\n\n" + self.jarvis._context_block())

    # ---- i tre compiti di un collegamento ------------------------------------
    async def _invia(self, s):
        """Dalla coda a Google: microfono, testo scritto, avvisi."""
        while True:
            tipo, dato, t = await self.coda.get()
            if tipo == "fine":
                # Stesso motivo del ricollegamento: lo stato si salva poco dopo
                # la fine del turno. Restando collegati ancora un istante (in
                # silenzio: la HUD ha gia' chiuso) la prossima conversazione
                # ripartira' da qui, ultimo scambio compreso.
                resto = 2.5 - (time.monotonic() - self._ultimo_turno)
                if self._ultimo_turno and 0 < resto and self._handle_t <= self._ultimo_turno:
                    await asyncio.sleep(resto)
                raise _Fine()
            if tipo == "audio":
                if self._muto or time.monotonic() - t > _AUDIO_SCADUTO_S:
                    continue
                await s.send_realtime_input(
                    audio=types.Blob(data=dato, mime_type="audio/pcm;rate=16000"))
            elif tipo == "testo":
                self._attivita()
                await s.send_realtime_input(text=dato)
            elif tipo == "annuncio":
                self._annunci.append(dato)
                await self._consegna(s)

    async def _ricevi(self, s):
        """Da Google alla HUD. receive() si ferma a ogni fine turno: si riparte."""
        while True:
            async for m in s.receive():
                await self._gestisci(s, m)

    async def _orologio(self, s):
        """Controlli periodici: avvisi in attesa, ricollegamento, inattivita'."""
        while True:
            await asyncio.sleep(0.5)
            if not self.canale.aperto:
                self.motivo = self.motivo or "HUD scollegata"
                raise _Fine()
            if self._annunci:
                await self._consegna(s)
            if self._go_away and self._pronto_a_ricollegarsi():
                raise _Ricollega()
            # Rete di sicurezza: di norma chiude la HUD, che sa quando l'audio
            # ha finito di suonare. Se la HUD si e' bloccata, chiudiamo noi.
            if (not self._strumenti
                    and time.monotonic() - self._ultima_attivita > max(90, SILENZIO_S * 3)):
                self.motivo = self.motivo or "silenzio"
                raise _Fine()

    # ---- messaggi da Google ------------------------------------------------------
    async def _gestisci(self, s, m):
        ripresa = m.session_resumption_update
        if ripresa and ripresa.resumable and ripresa.new_handle:
            self._handle = ripresa.new_handle
            self._handle_t = time.monotonic()
            _ricorda(self._handle, self.voce, self._istruzioni)

        if m.go_away:
            secondi = _secondi(m.go_away.time_left, 30.0)
            self._go_away = True
            # Margine di qualche secondo: ricollegarsi prima che Google tagli.
            self._go_away_entro = time.monotonic() + max(1.0, secondi - 5.0)
            print(f"[jarvis][live] Google chiudera' il collegamento fra {secondi:.0f}s: "
                  "mi ricollego appena c'e' silenzio.")

        if m.usage_metadata and m.usage_metadata.total_token_count:
            self._token += m.usage_metadata.total_token_count

        if m.tool_call:
            for fc in m.tool_call.function_calls or []:
                self._strumenti[fc.id] = fc.name
                self._lancia(self._strumento(s, fc))

        if m.tool_call_cancellation:
            for id_ in m.tool_call_cancellation.ids or []:
                self._annullati.add(id_)
                if self._strumenti.get(id_) == "use_computer":
                    brain.FRENO_PILOTA.set()

        sc = m.server_content
        if not sc:
            return
        if sc.interrupted:
            # Il signore ha parlato sopra Jarvis: Google ha gia' scartato il
            # resto della frase, la HUD deve buttare quello che ha in coda.
            self._fine_audio = time.monotonic()
            self._hud({"type": "live_interrupt"})
        if sc.input_transcription and sc.input_transcription.text:
            self._ultima_voce = time.monotonic()
            self._attivita()
            _ON_ATTIVITA()
            self._hud({"type": "live_user", "text": sc.input_transcription.text})
        if sc.model_turn:
            for p in sc.model_turn.parts or []:
                if p.inline_data and p.inline_data.data:
                    self._segna_genera()
                    self._audio_hud(p.inline_data.data)
        if sc.output_transcription and sc.output_transcription.text:
            self._segna_genera()
            self._hud({"type": "live_jarvis", "text": sc.output_transcription.text})
        if sc.turn_complete:
            self._genera = False
            self._ultimo_turno = time.monotonic()
            self._attivita()
            self._hud({"type": "live_turn"})
            if self._congedo == "risposto":
                self._congedo = None
                self._hud({"type": "live_end"})
            elif self._annunci:
                await self._consegna(s)

    # ---- strumenti -----------------------------------------------------------------
    async def _strumento(self, s, fc):
        nome = fc.name or ""
        args = dict(fc.args or {})
        self._attivita()
        self._hud({"type": "action",
                   "text": _ETICHETTE.get(nome) or brain._ACTION_LABEL.get(nome, nome)})
        self._hud({"type": "live_tool", "stato": "inizio", "nome": nome})
        if nome == "use_computer":
            # Il pilota racconta le mosse con la voce classica dalle casse: se
            # il microfono restasse aperto verso Google, quella voce potrebbe
            # passare per il signore che parla, e annullare il pilota. Per
            # fermarlo resta "basta", ascoltato dalla HUD.
            self._muto = True
        try:
            if nome == "end_conversation":
                self._congedo = "chiesto"
                out = {"status": "ok"}
            elif nome == "get_datetime":
                out = _data_ora()
            else:
                # Schermata compresa: e' lo stesso PNG del cervello classico e
                # del pilota. Una variante JPEG, piu' leggera da spedire, e'
                # stata provata e scartata: Windows Defender blocca lo script
                # di cattura con il codificatore JPEG come "contenuto dannoso".
                out = await asyncio.get_running_loop().run_in_executor(
                    None, self.jarvis.esegui_strumento, nome, args)
        except Exception as e:
            out = {"status": "error", "message": str(e)}
        finally:
            self._strumenti.pop(fc.id, None)
            if nome == "use_computer":
                self._muto = False
            self._attivita()
            self._hud({"type": "live_tool", "stato": "fine", "nome": nome})

        if fc.id in self._annullati or self._sessione is not s:
            # Annullata (il signore ha parlato sopra) o collegamento cambiato:
            # la risposta non e' piu' attesa da nessuno.
            self._annullati.discard(fc.id)
            return
        try:
            await self._rispondi(s, fc, out)
        except Exception as e:
            print(f"[jarvis][live] risposta dello strumento {nome} non consegnata: "
                  f"{brain.redact(e)}")
            return
        if self._congedo == "chiesto" and nome == "end_conversation":
            self._congedo = "risposto"
            self._lancia(self._congedo_di_riserva())

    async def _rispondi(self, s, fc, out):
        if isinstance(out, dict) and out.get("image_base64"):
            # L'immagine viaggia DENTRO la risposta, come nel cervello classico:
            # e' l'unico modo in cui il modello dal vivo la guarda davvero (un
            # fotogramma video separato lo ignora: provato). L'SDK installato
            # non sa serializzare i byte in questo punto, quindi il messaggio
            # si compone a mano, nel formato del protocollo.
            ws = getattr(s, "_ws", None)
            if ws is not None:
                await ws.send(json.dumps({"toolResponse": {"functionResponses": [{
                    "id": fc.id, "name": fc.name,
                    "response": {"status": "ok",
                                 "note": "Schermo catturato: guarda l'immagine."},
                    "parts": [{"inlineData": {"mimeType": out.get("media_type", "image/png"),
                                              "data": out["image_base64"]}}],
                }]}}))
                return
            out = {"status": "error", "message": "Immagine dello schermo non inviabile."}
        try:
            json.dumps(out)
            payload = out if isinstance(out, dict) else {"result": out}
        except (TypeError, ValueError):
            payload = {"result": str(out)}
        await s.send_tool_response(function_responses=[types.FunctionResponse(
            id=fc.id, name=fc.name, response=payload)])

    async def _congedo_di_riserva(self):
        """Se dopo il saluto il modello non chiude il turno, chiudiamo noi."""
        await asyncio.sleep(5)
        if self._congedo == "risposto" and not self._genera:
            self._congedo = None
            self._hud({"type": "live_end"})

    def _pronto_a_ricollegarsi(self):
        """Dopo un GoAway: e' il momento giusto per cambiare collegamento?"""
        adesso = time.monotonic()
        if adesso >= (self._go_away_entro or adesso):
            return True          # il tempo stringe: meglio un ricollegamento che una caduta
        if not self._quieto():
            return False
        # Google salva lo stato della sessione poco DOPO la fine di un turno:
        # ricollegandosi nell'istante in cui il turno si chiude, la sessione
        # ripresa a volte non conteneva l'ultimo scambio (provato: Jarvis aveva
        # dimenticato la parola appena detta). Si aspetta una maniglia piu'
        # recente dell'ultimo turno, oppure un paio di secondi di quiete.
        return self._handle_t > self._ultimo_turno or adesso - self._ultimo_turno > 2.5

    # ---- avvisi -------------------------------------------------------------------
    def _quieto(self):
        """Nessuno sta parlando e nessuno strumento e' in corso."""
        adesso = time.monotonic()
        # Se un turno non si chiude (il modello puo' decidere di tacere), dopo
        # un po' lo si considera finito, altrimenti gli avvisi resterebbero
        # bloccati per sempre.
        genera = self._genera and adesso - self._genera_dal < 20
        return (not genera and not self._strumenti
                and adesso > self._fine_audio + 0.4
                and adesso - self._ultima_voce > 1.5)

    async def _consegna(self, s):
        """Fa dire a Jarvis il primo avviso in attesa, se c'e' silenzio."""
        if not self._annunci or not self._quieto():
            return
        testo = self._annunci.pop(0)
        self._segna_genera()        # uno alla volta: il prossimo dopo questo turno
        self._attivita()
        await s.send_realtime_input(text="[AVVISO DI SISTEMA] " + testo)

    # ---- piccoli aiuti ----------------------------------------------------------
    def _segna_genera(self):
        if not self._genera:
            self._genera = True
            self._genera_dal = time.monotonic()
        self._attivita()

    def _attivita(self):
        self._ultima_attivita = time.monotonic()

    def _hud(self, obj):
        self.canale.send_json({**obj, "sid": self.sid})

    def _audio_hud(self, pcm):
        adesso = time.monotonic()
        # 24 kHz, 16 bit mono = 48000 byte al secondo.
        self._fine_audio = max(adesso, self._fine_audio) + len(pcm) / 48000.0
        self.canale.send_bytes(pcm)


# ---- registro delle conversazioni: l'interfaccia per app.py -------------------

_conversazioni = {}
_reg_lock = threading.Lock()


def _rimuovi(conv):
    with _reg_lock:
        if _conversazioni.get(conv.canale) is conv:
            del _conversazioni[conv.canale]


def _di(canale):
    with _reg_lock:
        c = _conversazioni.get(canale)
    return c if c is not None and not c.chiusa else None


def avvia(jarvis, canale, testo=None, voce=None, sid=0):
    """Apre la conversazione numero sid per questa HUD.

    Se e' gia' aperta le passa il testo. Se ce n'e' una con un altro numero,
    la HUD l'ha gia' data per chiusa: la si chiude e si apre quella nuova.
    """
    vecchia = None
    with _reg_lock:
        c = _conversazioni.get(canale)
        if c is not None and not c.chiusa:
            if c.sid == sid:
                if testo:
                    c.testo(testo)
                return c
            vecchia = c
        c = Conversazione(jarvis, canale, voce_valida(voce), _rimuovi, sid)
        _conversazioni[canale] = c
    if vecchia is not None:
        vecchia.ferma("sostituita")
    c.avvia(testo)
    return c


def audio(canale, pcm):
    c = _di(canale)
    if c is not None:
        c.audio(pcm)


def testo(canale, t, sid=0):
    """Testo scritto dentro una conversazione gia' aperta."""
    c = _di(canale)
    if c is not None and c.sid == sid:
        c.testo(t)
        return True
    # La conversazione non c'e' piu' (chiusa per silenzio mentre il signore
    # scriveva): lo si dice alla HUD, che si rimette in pari.
    canale.send_json({"type": "live_state", "state": "closed", "reason": "scaduta", "sid": sid})
    return False


def ferma(canale, motivo="hud", sid=None):
    c = _di(canale)
    if c is not None and (sid is None or c.sid == sid):
        c.ferma(motivo)


def annuncia(testo):
    """Un avviso detto dalla voce dal vivo, se c'e' una conversazione aperta."""
    with _reg_lock:
        aperte = [c for c in _conversazioni.values() if c.aperta and not c.chiusa]
    if not aperte:
        return False
    aperte[0].annuncia(testo)
    return True


def chiudi_tutto():
    with _reg_lock:
        tutte = list(_conversazioni.values())
    for c in tutte:
        c.ferma("arresto")
