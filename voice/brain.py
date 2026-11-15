"""Il cervello di Jarvis — Google Gemini (AI Studio).

Con una API key di Google AI Studio usa Gemini con function calling per ragionare
ed eseguire azioni sul PC. Senza key, un fallback locale gestisce comandi diretti.

La chiave viene letta SOLO da variabile d'ambiente (.env) dentro questo processo:
non passa mai dal browser.
"""
import base64
import datetime
import json
import os
import socket
import threading
import time
import re

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

try:
    import httpx                  # il trasporto dell'SDK: serve a riconoscerne gli errori di rete
except ImportError:
    httpx = None

import intents
import pilota
import quota
import tools
import webtools

# Il pilota automatico racconta a voce cosa sta per fare, e si ferma se il
# signore dice basta. Il server collega qui la voce della HUD e il freno; senza
# collegamento lavora in silenzio, che va bene per le prove ma non per l'uso.
NARRATORE = lambda testo: None          # noqa: E731
FRENO_PILOTA = threading.Event()

MODEL = os.environ.get("JARVIS_MODEL", "gemini-3.6-flash")

# Comandi semplici serviti in locale, senza consumare quota (vedi intents.py).
FAST_INTENTS = os.environ.get("JARVIS_FAST_INTENTS", "on").strip().lower() != "off"
MAX_TOOL_ROUNDS = 12

# Il piano gratuito di Gemini limita le richieste PER MODELLO (es. 20 al giorno).
# Quando un modello esaurisce la quota (429) passiamo al successivo: cosi le
# richieste disponibili si moltiplicano. Ogni modello ha secchielli separati.
#
# ORDINE = velocita. Quando il primario esaurisce la quota, il ripiego deve
# essere RAPIDO: i modelli '-lite' rispondono in ~0.5s, mentre gemini-3.5-flash,
# col suo ragionamento di default, arriva a 15-30s a chiamata (misurato). Quindi
# i lite vengono prima, e i flash "pensanti" restano come ultima risorsa. Senza
# questo, l'esaurimento del primario faceva crollare la reattivita a picco.
_FALLBACK_MODELS = [
    "gemini-3.6-flash",          # primario: piu capace quando ha quota
    "gemini-flash-lite-latest",  # ripiego RAPIDO (~0.5s), niente thinking
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
    "gemini-3.5-flash",          # capace ma LENTO (thinking): ultima risorsa
    "gemini-3-flash-preview",
]
_env_fallbacks = os.environ.get("JARVIS_FALLBACK_MODELS", "").strip()
if _env_fallbacks:
    _FALLBACK_MODELS = [m.strip() for m in _env_fallbacks.split(",") if m.strip()]

# Ricerca web integrata di Google: "auto" prova ad attivarla e si disattiva da
# sola se il modello scelto non accetta ricerca e function calling insieme.
WEB_SEARCH = os.environ.get("JARVIS_WEB_SEARCH", "auto").strip().lower()


class QuotaError(Exception):
    """Tutti i modelli disponibili hanno esaurito la quota gratuita."""


class BusyError(Exception):
    """I server di Google sono momentaneamente sovraccarichi (503)."""


class NetworkError(Exception):
    """Google non raggiungibile: DNS che non risolve, connessione rifiutata o caduta."""


# Frase detta quando la rete non lascia passare la richiesta nemmeno dopo i
# tentativi. Identica a quella in speech.FRASI_FISSE, che la prepara in anticipo:
# a rete giu' la sintesi vocale non si potrebbe fare.
MSG_RETE = ("Non riesco a raggiungere i server di Google: la connessione o il DNS "
            "non rispondono. Riprova tra qualche secondo.")

# Attese fra i tentativi quando a fallire e' la RETE (secondi): circa 5 s in
# tutto, poi ci si arrende con un messaggio chiaro.
_ATTESE_RETE = (0.6, 1.5, 3.0)


def _is_network(e):
    """Errore di RETE: Google non e' stato raggiunto, o la risposta si e' persa.

    Diverso da 429 e 503, che sono RISPOSTE di Google. Il caso tipico qui e'
    "[Errno 11001] getaddrinfo failed": il DNS non ha risolto il nome del server.
    Misurato su questa rete: il DNS del router restituisce ogni tanto pacchetti
    corrotti (2-4% delle domande) e ogni volta il turno falliva in 0,00 s.
    L'SDK solleva httpx.ConnectError e non ritenta da solo.
    """
    if httpx is not None and isinstance(e, httpx.TransportError):
        return True
    if isinstance(e, (socket.gaierror, ConnectionError)):
        return True
    s = str(e).lower()
    return "getaddrinfo failed" in s or "name resolution" in s


def _network_retryable(e):
    """Si puo' ritentare SUBITO? Si' per gli errori che arrivano in fretta (DNS,
    connessione rifiutata o caduta); no per i timeout di lettura e scrittura, che
    hanno gia' fatto aspettare a lungo: ritentarli raddoppierebbe l'attesa."""
    if httpx is not None and isinstance(e, (httpx.ReadTimeout, httpx.WriteTimeout,
                                            httpx.PoolTimeout)):
        return False
    return True


def _is_quota(e):
    s = str(e)
    return "RESOURCE_EXHAUSTED" in s or "'code': 429" in s or "code: 429" in s


def _is_transient(e):
    """Errore server temporaneo (sovraccarico): conviene cambiare modello o ritentare."""
    s = str(e).lower()
    return ("503" in s or "unavailable" in s or "high demand" in s
            or "overloaded" in s or "'code': 500" in s or "internal error" in s)


def _is_bad_request(e):
    """Richiesta rifiutata dal modello (400 INVALID_ARGUMENT).

    L'unico parametro fuori standard che aggiungiamo e' il budget di ragionamento,
    e diversi modelli 'lite' lo rifiutano con un 400 dal messaggio generico (non
    cita 'thinking'). Quando capita lo togliamo per quel modello e riproviamo,
    invece di far fallire il turno.
    """
    s = str(e)
    return ("INVALID_ARGUMENT" in s or "'code': 400" in s or "code: 400" in s)

# Budget di ragionamento. Vuoto = default del modello; 0 = disattivato (piu veloce).
_BUDGET = os.environ.get("JARVIS_THINKING_BUDGET", "").strip()

SYSTEM = """Sei Jarvis, maggiordomo digitale, ispirato all'assistente AI di Iron Man: capace, leale, misurato, con un tocco di ironia asciutta quando serve.

COME TI RIVOLGI A LUI:
- Dai del LEI e chiamalo "signore". Non usare MAI il suo nome proprio.
- Usa "signore" con parsimonia: al massimo in una frase su tre, e quasi sempre all'inizio ("Certamente, signore.") oppure per nulla. NON chiudere ogni risposta con "signore": suona servile e ripetitivo. La maggior parte delle tue risposte non deve contenere alcun appellativo.
- Registro sobrio e professionale, mai ossequioso.

Controlli un PC Windows 11 (home C:\\Users\\Utente) e hai accesso completo al sistema: app, file, comandi PowerShell, audio e multimedia, finestre, tastiera, appunti, schermo, promemoria pianificati e una memoria a lungo termine.

App installate: VS Code, Firefox, Chrome, Edge, Node, Git. Percorsi utili:
- VS Code: %LOCALAPPDATA%\\Programs\\Microsoft VS Code\\Code.exe
- Firefox: C:\\Program Files\\Mozilla Firefox\\firefox.exe
Progetti: C:\\Users\\Utente\\worldcup-predictor (predittore World Cup) e C:\\Users\\Utente\\Jarvis (questo assistente).

COME PARLI:
- Le tue risposte vengono LETTE AD ALTA VOCE in italiano. Quindi: frasi brevi, naturali, parlato scorrevole. NIENTE markdown, elenchi puntati, blocchi di codice, asterischi, emoji o simboli. Massimo una o due frasi, salvo che ti venga chiesto esplicitamente un approfondimento.
- Numeri, orari e unita vanno scritti come si pronunciano ("le quindici e trenta", "ottanta per cento").
- Quando esegui un'azione, dillo in modo breve e concreto: "Fatto, ho aperto Firefox".
- Non annunciare cosa stai per fare: fallo e poi riferisci il risultato.

COME AGISCI:
- Preferisci agire invece di chiedere. Se un dettaglio minore e ambiguo (quale cartella, quale nome file), scegli l'opzione piu sensata e dillo, invece di fermarti a domandare.
- Per informazioni in tempo reale (meteo, notizie, prezzi, risultati, orari) usa la ricerca web: non rispondere a memoria.
- Se hai bisogno di sapere cosa c'e sullo schermo, usa lo strumento screenshot e guarda davvero l'immagine che ti viene restituita.
- Se un comando non esiste come strumento dedicato, costruiscilo con run_powershell: hai pieno accesso.
- Quando il signore dice qualcosa di duraturo su di se, sulle sue preferenze o sul suo lavoro, salvalo con remember.

SICUREZZA:
- Per azioni distruttive o irreversibili (cancellare file, terminare processi, spegnere o riavviare, disinstallare, modificare il registro) NON procedere subito. Spiega in una frase cosa succedera e chiedi conferma esplicita. Solo dopo un si chiaro del signore richiama lo stesso strumento con confirm=true.
- Se uno strumento risponde needs_confirmation, non insistere: riporta la richiesta di conferma al signore e aspetta.
- Se qualcosa fallisce o non e fattibile, dillo con onesta e spiega il perche in breve. Non inventare mai un risultato che non hai verificato."""


# ---- Definizione degli strumenti (formato neutro, poi tradotto per Gemini) ---

def _spec(name, description, properties=None, required=None):
    return {"name": name, "description": description,
            "properties": properties or {}, "required": required or []}


TOOL_SPECS = [
    _spec("run_powershell",
          "Esegue un comando PowerShell su Windows: controllo completo del PC. Usalo per "
          "qualsiasi cosa non coperta dagli altri strumenti. Per comandi distruttivi passa "
          "confirm=true SOLO dopo conferma esplicita dell'utente.",
          {"command": {"type": "string", "description": "Il comando PowerShell da eseguire."},
           "confirm": {"type": "boolean", "description": "true solo dopo conferma esplicita."},
           "timeout": {"type": "integer", "description": "Timeout in secondi (default 30, max 120)."}},
          ["command"]),

    _spec("open_app", "Apre un'applicazione per nome (es. chrome, firefox, vs code, spotify).",
          {"name": {"type": "string"}}, ["name"]),
    _spec("open_path", "Apre un file o una cartella con l'app predefinita.",
          {"path": {"type": "string"}}, ["path"]),
    _spec("open_url", "Apre un indirizzo web nel browser predefinito.",
          {"url": {"type": "string"}}, ["url"]),

    _spec("web_search",
          "Cerca sul web e restituisce titoli, link e riassunti. USALO SEMPRE per "
          "notizie, risultati sportivi, prezzi, orari e qualunque informazione "
          "aggiornata: non rispondere mai a memoria su fatti recenti.",
          {"query": {"type": "string"},
           "max_results": {"type": "integer", "description": "Default 6."}},
          ["query"]),
    _spec("weather",
          "Meteo attuale e previsione per una citta. Senza citta usa la posizione "
          "rilevata dalla connessione.",
          {"location": {"type": "string", "description": "Nome della citta."},
           "days": {"type": "integer", "description": "Giorni di previsione (default 1)."}}),
    _spec("fetch_page",
          "Scarica una pagina web e ne restituisce il testo, per leggerla o riassumerla.",
          {"url": {"type": "string"},
           "max_chars": {"type": "integer", "description": "Default 5000."}},
          ["url"]),

    _spec("list_directory", "Elenca i file di una cartella.",
          {"path": {"type": "string"}}, ["path"]),
    _spec("search_files", "Cerca file per nome nel PC, ricorsivamente.",
          {"query": {"type": "string", "description": "Parte del nome del file."},
           "root": {"type": "string", "description": "Cartella di partenza (default: home utente)."}},
          ["query"]),
    _spec("read_file", "Legge il contenuto di un file di testo.",
          {"path": {"type": "string"},
           "max_chars": {"type": "integer", "description": "Caratteri massimi (default 6000)."}},
          ["path"]),
    _spec("write_file",
          "Scrive o accoda testo su un file. Sovrascrivere un file esistente richiede confirm=true.",
          {"path": {"type": "string"}, "content": {"type": "string"},
           "append": {"type": "boolean", "description": "true per accodare invece di sovrascrivere."},
           "confirm": {"type": "boolean"}},
          ["path", "content"]),

    _spec("system_status", "Stato del sistema: CPU, RAM, dischi, uptime, batteria, rete."),
    _spec("list_processes", "Elenca i processi attivi piu pesanti.",
          {"sort_by": {"type": "string", "enum": ["memory", "cpu"]},
           "top": {"type": "integer"}}),
    _spec("kill_process", "Termina un processo per nome o PID. Richiede conferma.",
          {"name_or_pid": {"type": "string"}, "confirm": {"type": "boolean"}},
          ["name_or_pid"]),
    _spec("power_control",
          "Blocca, sospende, riavvia, spegne il PC o disconnette l'utente. "
          "Tutto tranne 'lock' richiede conferma.",
          {"action": {"type": "string", "enum": ["lock", "sleep", "restart", "shutdown", "logoff"]},
           "confirm": {"type": "boolean"}},
          ["action"]),

    _spec("set_volume",
          "Regola il volume di sistema. Usa level (0-100) per un valore assoluto, "
          "delta (es. 20 o -20) per alzare/abbassare, mute per silenziare.",
          {"level": {"type": "integer"}, "delta": {"type": "integer"},
           "mute": {"type": "boolean"}}),
    _spec("media_control", "Controlla la riproduzione multimediale in corso.",
          {"action": {"type": "string", "enum": ["play_pause", "next", "previous", "stop"]}},
          ["action"]),

    _spec("screenshot",
          "Cattura lo schermo e te lo mostra come immagine: usalo quando devi VEDERE "
          "cosa c'e sullo schermo in questo momento."),
    _spec("type_text",
          "Scrive del testo nella finestra in primo piano, come se lo digitasse il "
          "signore. ATTENZIONE: scrive dove c'e il fuoco, quindi metti prima a fuoco la "
          "finestra giusta con open_app o window_control. Passa expect_window con una "
          "parte del titolo atteso: se non corrisponde non scrive nulla, cosi non "
          "rischi di sporcare un documento aperto.",
          {"text": {"type": "string"},
           "expect_window": {"type": "string",
                             "description": "Parte del titolo della finestra attesa."}},
          ["text"]),
    _spec("send_message",
          "Apre la chat con un contatto su WhatsApp, Telegram, Slack o Discord e ci "
          "scrive dentro il messaggio. Di default NON invia: lascia il testo pronto da "
          "rileggere. Per inviare davvero servono send=true e confirm=true dopo una "
          "conferma esplicita del signore.",
          {"app": {"type": "string", "enum": ["whatsapp", "telegram", "slack", "discord"]},
           "contact": {"type": "string", "description": "Nome del contatto o della chat."},
           "message": {"type": "string"},
           "send": {"type": "boolean", "description": "true per premere Invio e inviare."},
           "confirm": {"type": "boolean"}},
          ["app", "contact", "message"]),
    _spec("press_keys",
          "Invia una scorciatoia da tastiera in sintassi SendKeys. Esempi: '^s' (Ctrl+S), "
          "'%{F4}' (Alt+F4), '{ENTER}'.",
          {"keys": {"type": "string"}}, ["keys"]),
    _spec("window_to_monitor",
          "Sposta la finestra attiva sullo schermo a sinistra o a destra "
          "(utile con piu monitor). Conserva dimensione e stato massimizzato.",
          {"direction": {"type": "string", "enum": ["left", "right"]}}, ["direction"]),
    _spec("window_control", "Gestisce le finestre aperte.",
          {"action": {"type": "string", "enum": ["focus", "minimize_all", "restore_all", "list"]},
           "title": {"type": "string", "description": "Titolo parziale, richiesto per focus."}},
          ["action"]),
    _spec("clipboard", "Legge o scrive gli appunti di Windows.",
          {"action": {"type": "string", "enum": ["read", "write"]}, "text": {"type": "string"}},
          ["action"]),
    _spec("notify", "Mostra una notifica sul desktop di Windows.",
          {"title": {"type": "string"}, "message": {"type": "string"}}, ["message"]),

    _spec("schedule_reminder",
          "Programma un promemoria che suonera a un orario preciso, anche se Jarvis viene "
          "riavviato. Converti sempre espressioni come 'domani alle 9' in data e ora assolute.",
          {"text": {"type": "string"},
           "when": {"type": "string", "description": "Formato 'AAAA-MM-GG HH:MM'."}},
          ["text", "when"]),
    _spec("list_reminders", "Elenca i promemoria pianificati."),
    _spec("cancel_reminder", "Annulla un promemoria pianificato dato il suo nome.",
          {"task_name": {"type": "string"}}, ["task_name"]),

    _spec("add_todo", "Aggiunge una voce alla to-do list.", {"text": {"type": "string"}}, ["text"]),
    _spec("list_todos", "Mostra la to-do list."),
    _spec("add_note", "Salva una nota rapida con data e ora.",
          {"text": {"type": "string"}}, ["text"]),

    _spec("remember",
          "Memorizza in modo permanente un fatto sul signore, le sue preferenze o il suo "
          "lavoro. Usalo quando emerge qualcosa che ti servira nelle sessioni future.",
          {"fact": {"type": "string"}}, ["fact"]),
    _spec("forget", "Cancella dalla memoria i fatti che contengono un certo testo.",
          {"fragment": {"type": "string"}}, ["fragment"]),

    _spec("use_computer",
          "PILOTA AUTOMATICO: usa il computer al posto del signore guardando lo schermo. "
          "Apre programmi, clicca pulsanti, compila campi e verifica il risultato a ogni "
          "passo. Usalo SOLO quando il compito richiede di navigare a mano un'interfaccia "
          "grafica che nessun altro strumento sa raggiungere. Se esiste uno strumento "
          "diretto (open_app, web_search, set_volume...) usa quello: e istantaneo, "
          "gratuito e non puo sbagliare mira. Il pilota non compie azioni irreversibili: "
          "niente acquisti, invii, cancellazioni o password.",
          {"obiettivo": {"type": "string",
                         "description": "Il compito, descritto come lo diresti a una persona."},
           "max_passi": {"type": "integer",
                         "description": "Tetto di mosse. Predefinito 12."}},
          ["obiettivo"]),
]

DISPATCH = {
    "run_powershell": lambda i: tools.run_powershell(
        i.get("command", ""), i.get("confirm", False), i.get("timeout", 30)),
    "open_app": lambda i: tools.open_app(i.get("name", "")),
    "open_path": lambda i: tools.open_path(i.get("path", "")),
    "open_url": lambda i: tools.open_url(i.get("url", "")),
    "web_search": lambda i: webtools.web_search(i.get("query", ""), i.get("max_results", 6)),
    "weather": lambda i: webtools.weather(i.get("location", ""), i.get("days", 1)),
    "fetch_page": lambda i: webtools.fetch_page(i.get("url", ""), i.get("max_chars", 5000)),
    "list_directory": lambda i: tools.list_directory(i.get("path", "")),
    "search_files": lambda i: tools.search_files(i.get("query", ""), i.get("root")),
    "read_file": lambda i: tools.read_file(i.get("path", ""), i.get("max_chars", 6000)),
    "write_file": lambda i: tools.write_file(
        i.get("path", ""), i.get("content", ""), i.get("append", False), i.get("confirm", False)),
    "system_status": lambda i: tools.system_status(),
    "list_processes": lambda i: tools.list_processes(i.get("sort_by", "memory"), i.get("top", 12)),
    "kill_process": lambda i: tools.kill_process(i.get("name_or_pid", ""), i.get("confirm", False)),
    "power_control": lambda i: tools.power_control(i.get("action", ""), i.get("confirm", False)),
    "set_volume": lambda i: tools.set_volume(i.get("level"), i.get("delta"), i.get("mute")),
    "media_control": lambda i: tools.media_control(i.get("action", "")),
    "screenshot": lambda i: tools.screenshot(),
    "type_text": lambda i: tools.type_text(i.get("text", ""), i.get("expect_window")),
    "send_message": lambda i: tools.send_message(
        i.get("app", "whatsapp"), i.get("contact", ""), i.get("message", ""),
        i.get("send", False), i.get("confirm", False)),
    "press_keys": lambda i: tools.press_keys(i.get("keys", "")),
    "window_control": lambda i: tools.window_control(i.get("action", ""), i.get("title")),
    "window_to_monitor": lambda i: tools.window_to_monitor(i.get("direction", "")),
    "clipboard": lambda i: tools.clipboard(i.get("action", ""), i.get("text")),
    "notify": lambda i: tools.notify(i.get("title"), i.get("message", "")),
    "schedule_reminder": lambda i: tools.schedule_reminder(i.get("text", ""), i.get("when", "")),
    "list_reminders": lambda i: tools.list_reminders(),
    "cancel_reminder": lambda i: tools.cancel_reminder(i.get("task_name", "")),
    "add_todo": lambda i: tools.add_todo(i.get("text", "")),
    "list_todos": lambda i: tools.list_todos(),
    "add_note": lambda i: tools.add_note(i.get("text", "")),
    "remember": lambda i: tools.remember(i.get("fact", "")),
    "forget": lambda i: tools.forget(i.get("fragment", "")),
    # Il pilota ha bisogno del client e della catena di modelli, che vivono
    # nell'istanza: viene quindi agganciato in _dispatch, non qui.
}

_ACTION_LABEL = {
    "use_computer": "Prendo il controllo",
    "run_powershell": "Eseguo un comando",
    "open_app": "Apro un'app",
    "open_path": "Apro un percorso",
    "open_url": "Apro il browser",
    "web_search": "Cerco sul web",
    "weather": "Controllo il meteo",
    "fetch_page": "Leggo la pagina",
    "list_directory": "Elenco i file",
    "search_files": "Cerco file",
    "read_file": "Leggo un file",
    "write_file": "Scrivo un file",
    "system_status": "Controllo il sistema",
    "list_processes": "Controllo i processi",
    "kill_process": "Termino un processo",
    "power_control": "Controllo l'alimentazione",
    "set_volume": "Regolo il volume",
    "media_control": "Controllo la riproduzione",
    "screenshot": "Guardo lo schermo",
    "type_text": "Scrivo del testo",
    "send_message": "Preparo un messaggio",
    "press_keys": "Invio una scorciatoia",
    "window_control": "Gestisco le finestre",
    "window_to_monitor": "Sposto la finestra",
    "clipboard": "Uso gli appunti",
    "notify": "Mostro una notifica",
    "schedule_reminder": "Imposto un promemoria",
    "list_reminders": "Controllo i promemoria",
    "cancel_reminder": "Annullo un promemoria",
    "add_todo": "Aggiungo un todo",
    "list_todos": "Leggo la to-do list",
    "add_note": "Salvo una nota",
    "remember": "Memorizzo",
    "forget": "Dimentico",
}

_GIORNI = ["lunedi", "martedi", "mercoledi", "giovedi", "venerdi", "sabato", "domenica"]

# Istruzioni aggiuntive quando la ricerca web di Google non e disponibile
# (quota del piano gratuito, o modello che non la combina con gli strumenti).
_NO_SEARCH_NOTE = """
NOTA: la ricerca integrata di Google non e attiva in questa sessione, ma hai
comunque accesso al web con i tuoi strumenti: usa web_search per cercare,
weather per il meteo e fetch_page per leggere una pagina. Usali sempre per
notizie, risultati, prezzi e qualunque dato aggiornato: non rispondere a memoria
e non inventare mai."""


def _api_key():
    """Chiave di Google AI Studio: accetta entrambi i nomi usati da Google."""
    return (os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or "").strip()


def redact(text):
    """Toglie la chiave da qualunque testo destinato a log o interfaccia.

    Oggi l'SDK non la include nei messaggi d'errore, ma un aggiornamento
    potrebbe cambiarlo: meglio filtrare comunque prima di stampare.
    """
    out = str(text)
    key = _api_key()
    if key and key in out:
        out = out.replace(key, "***CHIAVE-REDATTA***")
    return re.sub(r"(?i)(key=)[\w.\-]{8,}", r"\1***REDATTA***", out)


def _declarations():
    """Traduce le specifiche neutre in FunctionDeclaration per Gemini."""
    out = []
    for s in TOOL_SPECS:
        params = None
        if s["properties"]:
            # Gemini rifiuta un oggetto parametri vuoto: se non ci sono
            # argomenti, la dichiarazione va inviata senza schema.
            params = {"type": "object", "properties": s["properties"]}
            if s["required"]:
                params["required"] = s["required"]
        out.append(types.FunctionDeclaration(
            name=s["name"], description=s["description"], parameters=params))
    return out


class Jarvis:
    def __init__(self):
        self.model = MODEL
        key = _api_key()
        self.has_key = bool(key) and genai is not None
        # Il client tiene la chiave in questo processo: non la logghiamo mai.
        self.client = genai.Client(api_key=key) if self.has_key else None
        self.contents = []
        self._decls = _declarations() if self.has_key else []
        self._search_on = self.has_key and WEB_SEARCH in ("auto", "on")
        # Il budget di ragionamento serve solo ai modelli che "pensano" (i flash):
        # i 'lite' non lo accettano (400). Lo passiamo quindi solo a chi lo regge,
        # e ricordiamo qui i modelli che lo rifiutano, per non riprovarci.
        self._thinking_on = True
        self._thinking_bad = set()

    # ---- entry point -------------------------------------------------------
    def process(self, text, on_status=None, on_action=None, on_say=None):
        st = on_status or (lambda s: None)
        ac = on_action or (lambda a: None)
        say = on_say or (lambda t: None)

        # Scorciatoia: i comandi semplici li serviamo qui, senza chiamare il
        # modello. Risposta immediata e nessuna richiesta consumata.
        if FAST_INTENTS:
            diretto = intents.match(text)
            if diretto is not None:
                frase, azione = diretto
                if azione:
                    ac(azione)
                # Registriamo lo scambio nella cronologia, cosi il modello sa
                # cosa e successo se il turno dopo dice "e adesso alzalo ancora".
                self._record(text, frase)
                st("speaking")
                say(frase)
                return frase

        if self.has_key:
            return self._gemini(text, st, ac, say)
        return self._local(text, st, ac, say)

    def _record(self, user_text, reply):
        """Annota nella cronologia uno scambio gestito in locale."""
        if not self.has_key:
            return
        self.contents.append(
            types.Content(role="user", parts=[types.Part.from_text(text=user_text)]))
        self.contents.append(
            types.Content(role="model", parts=[types.Part.from_text(text=reply)]))
        self._trim()

    def reset(self):
        self.contents = []

    # ---- contesto dinamico -------------------------------------------------
    def _context_block(self):
        now = datetime.datetime.now()
        lines = [f"Adesso e {_GIORNI[now.weekday()]} {now.day}/{now.month}/{now.year}, "
                 f"ore {now:%H:%M}."]
        mem = tools.memory_digest()
        if mem:
            lines.append("Cosa ricordi del signore dalle sessioni precedenti:\n" + mem)
        return "\n\n".join(lines)

    def _config(self, model=""):
        tool_list = [types.Tool(function_declarations=self._decls)]
        if self._search_on:
            tool_list.append(types.Tool(google_search=types.GoogleSearch()))
        instruction = SYSTEM
        if not self._search_on:
            instruction += "\n" + _NO_SEARCH_NOTE
        cfg = {
            "system_instruction": instruction + "\n\n" + self._context_block(),
            "tools": tool_list,
            "max_output_tokens": 4000,
            # Il ciclo dei tool lo guidiamo noi: serve per la HUD e per le conferme.
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
        }
        if self._thinking_supported(model):
            try:
                cfg["thinking_config"] = types.ThinkingConfig(thinking_budget=int(_BUDGET))
            except ValueError:
                pass
        return types.GenerateContentConfig(**cfg)

    def _thinking_supported(self, model):
        """Il budget di ragionamento va passato solo a chi lo accetta.

        Serve un budget configurato e non disattivato; il modello non dev'essere
        un 'lite' (non pensano e rifiutano il parametro con un 400) e non deve
        averlo gia rifiutato in questa sessione.
        """
        return (bool(_BUDGET) and self._thinking_on
                and "lite" not in model and model not in self._thinking_bad)

    def _model_chain(self):
        """Il modello configurato per primo, poi i fallback (senza duplicati)."""
        chain = [self.model]
        for m in _FALLBACK_MODELS:
            if m not in chain:
                chain.append(m)
        return chain

    def model_chain(self):
        """Catena di modelli (primario + ripieghi): per la HUD e la diagnostica."""
        return self._model_chain()

    def sonda_quota(self, model=None):
        """Una sola chiamata minima al modello primario, per rendere il pannello
        HUD realistico gia all'avvio: capisce subito se il primario e' esaurito.

        Costo controllato: se il tracker sa gia lo stato del modello per oggi,
        NON spende una richiesta. Gira in un thread: se la rete manca o il
        modello e' lento, non blocca l'avvio del server.
        """
        if not self.has_key:
            return
        model = model or self.model
        if quota.known(model):
            return                        # gia noto oggi: niente richiesta sprecata
        # Richiesta la piu spoglia possibile: niente thinking (i 'lite' lo
        # rifiutano) ne strumenti. Ci interessa solo se risponde o da' 429.
        t0 = time.perf_counter()
        try:
            self.client.models.generate_content(
                model=model, contents="ping",
                config=types.GenerateContentConfig(max_output_tokens=8))
            quota.record_success(model, time.perf_counter() - t0)
            print(f"[jarvis] sonda quota: {model} disponibile.")
        except Exception as e:
            if _is_quota(e):
                quota.record_exhausted(model)
                print(f"[jarvis] sonda quota: {model} esaurito (429).")
            # Altri errori (rete, sovraccarico): si scoprira con l'uso normale.

    def _raw_call(self, model):
        return self.client.models.generate_content(
            model=model, contents=self.contents, config=self._config(model))

    def _call_with_search_fallback(self, model):
        """Una chiamata al modello dato, con due reti di sicurezza.

        - Se il modello rifiuta la richiesta (400) e gli stavamo passando il
          budget di ragionamento, lo ricordiamo come "senza budget" e riproviamo
          senza: i modelli 'lite' non lo accettano, ma altri si, quindi il veto
          vale solo per quel modello.
        - Se fallisce con la ricerca web attiva, riprova una volta senza: alcuni
          modelli non combinano ricerca e strumenti, o la ricerca ha quota
          separata. Se la colpa non era la ricerca, ripristina.
        """
        try:
            return self._raw_call(model)
        except Exception as first:
            # Un inciampo della rete non c'entra con la ricerca web: senza questa
            # uscita, se la rete tornava proprio al secondo tentativo, la ricerca
            # veniva spenta per tutta la sessione per una colpa non sua.
            if _is_network(first):
                raise
            if self._thinking_supported(model) and _is_bad_request(first):
                self._thinking_bad.add(model)
                print(f"[jarvis] {model} non accetta il budget di ragionamento: "
                      "lo disattivo per questo modello.")
                return self._raw_call(model)
            if not (self._search_on and WEB_SEARCH == "auto"):
                raise
            self._search_on = False
            try:
                resp = self._raw_call(model)
            except Exception:
                self._search_on = True
                raise first
            print("[jarvis] ricerca web non disponibile su questa chiave o modello: "
                  "disattivata per questa sessione.")
            return resp

    def _call_resilient(self, model):
        """La chiamata al modello, ritentata se a fallire e' la RETE e non Google.

        Un DNS che inciampa un attimo non deve far fallire la richiesta: la
        domanda non e' mai arrivata a Google, quindi riprovare e' sicuro e non
        consuma quota. Si riprova sullo STESSO modello: tutti i modelli stanno
        sullo stesso server, cambiarlo non aggirerebbe un problema di rete.
        """
        for attesa in _ATTESE_RETE + (None,):
            try:
                return self._call_with_search_fallback(model)
            except Exception as e:
                if not _is_network(e):
                    raise
                if attesa is None or not _network_retryable(e):
                    raise NetworkError(str(e)) from e
                print(f"[jarvis] Google non raggiungibile ({redact(e)}): "
                      f"riprovo fra {attesa:.1f}s.")
                time.sleep(attesa)

    def _generate(self):
        """Chiama Gemini scorrendo la catena di modelli. Un modello che esaurisce la
        quota (429) o e sovraccarico (503) viene saltato e si prova il successivo, che
        ha secchielli separati. Il modello che risponde diventa quello di default per
        la sessione. Se tutta la catena e sovraccarica, dopo una breve pausa ritenta.
        Se restano solo errori di quota solleva QuotaError; se solo sovraccarichi,
        BusyError; altri errori vengono propagati subito."""
        last_quota = last_transient = None
        for attempt in range(2):          # una passata completa + un retry per il 503
            if attempt:
                time.sleep(1.2)           # il sovraccarico e transitorio: aspetta e riprova
            saw_transient = False
            for model in self._model_chain():
                t0 = time.perf_counter()
                try:
                    resp = self._call_resilient(model)
                except Exception as e:
                    if isinstance(e, NetworkError):
                        raise                 # gia' ritentato: e' la rete, non il modello
                    if _is_quota(e):
                        quota.record_exhausted(model)
                        last_quota = e
                        print(f"[jarvis] quota esaurita su {model}, provo un altro modello.")
                        continue
                    if _is_transient(e):
                        last_transient = e
                        saw_transient = True
                        print(f"[jarvis] {model} sovraccarico, provo un altro modello.")
                        continue
                    raise
                dt = time.perf_counter() - t0
                quota.record_success(model, dt)
                print(f"[jarvis][tempi] modello {model}: {dt:.2f}s")
                if model != self.model:
                    print(f"[jarvis] passo al modello {model} per questa sessione.")
                    self.model = model
                return resp
            if not saw_transient:
                break                     # solo quota: ritentare non aiuterebbe
        if last_transient is not None:
            raise BusyError(str(last_transient))
        raise QuotaError(str(last_quota) if last_quota else "quota esaurita")

    # ---- cervello Gemini ---------------------------------------------------
    def _gemini(self, text, st, ac, say):
        st("thinking")
        self.contents.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))
        turn_t0 = time.perf_counter()
        rounds = 0
        try:
            final = ""
            for _ in range(MAX_TOOL_ROUNDS):
                rounds += 1
                resp = self._generate()
                cand = (resp.candidates or [None])[0]
                if cand is None or cand.content is None:
                    final = self._blocked_message(resp, cand)
                    break

                self.contents.append(cand.content)
                parts = cand.content.parts or []
                calls = [p.function_call for p in parts if getattr(p, "function_call", None)]

                if not calls:
                    final = " ".join(
                        p.text.strip() for p in parts
                        if getattr(p, "text", None) and not getattr(p, "thought", False)
                    ).strip()
                    if not final:
                        final = self._blocked_message(resp, cand)
                    break

                results = []
                for fc in calls:
                    ac(_ACTION_LABEL.get(fc.name, fc.name))
                    results.append(self._run_tool(fc))
                self.contents.append(types.Content(role="user", parts=results))
                st("thinking")

            self._trim()
            if final:
                st("speaking")
                say(final)
            else:
                st("idle")
            return final
        except QuotaError as e:
            st("error")
            self._drop_incomplete_turn()
            msg = ("Mi spiace signore, ho esaurito le richieste gratuite di Gemini per oggi. "
                   "Il piano gratuito ne concede solo una ventina al giorno per modello. "
                   "Riprova piu tardi, oppure attiva la fatturazione su Google AI Studio "
                   "per non avere questo limite.")
            say(msg)
            print("[jarvis] quota gratuita esaurita su tutti i modelli: " + redact(e))
            return msg
        except BusyError as e:
            st("error")
            self._drop_incomplete_turn()
            msg = ("I server di Google sono momentaneamente sovraccarichi. "
                   "Riprova tra qualche secondo.")
            say(msg)
            print("[jarvis] modelli sovraccarichi (503): " + redact(e))
            return msg
        except NetworkError as e:
            st("error")
            self._drop_incomplete_turn()
            say(MSG_RETE)
            print("[jarvis] Google non raggiungibile nemmeno dopo i tentativi: " + redact(e))
            return MSG_RETE
        except Exception as e:
            st("error")
            # Un messaggio piu utile del generico "errore tecnico".
            detail = redact(e)
            if _is_quota(e):
                say("Sto andando troppo veloce per il piano gratuito. "
                    "Aspetta un momento e riprova.")
            else:
                say("Non sono riuscito a completare la richiesta. "
                    "Controlla la console per il dettaglio.")
            self._drop_incomplete_turn()
            print("[jarvis] errore nel turno: " + detail, file=__import__("sys").stderr)
            return "Errore: " + detail
        finally:
            dt = time.perf_counter() - turn_t0
            print(f"[jarvis][tempi] turno completo: {rounds} round, {dt:.2f}s")

    @staticmethod
    def _blocked_message(resp, cand):
        """Messaggio parlato quando Gemini non restituisce testo (filtri, limiti)."""
        reason = getattr(cand, "finish_reason", None) if cand else None
        fb = getattr(resp, "prompt_feedback", None)
        blocked = getattr(fb, "block_reason", None) if fb else None
        if blocked or (reason and str(reason).upper().endswith("SAFETY")):
            return "Su questo non posso esserle utile, signore: la richiesta e stata bloccata."
        if reason and "MAX_TOKENS" in str(reason).upper():
            return "La risposta era troppo lunga e si e interrotta."
        return ""

    def _pilota(self, args):
        """Avvia il pilota automatico riusando client e catena di modelli.

        Sta qui e non in DISPATCH perche' ha bisogno dell'istanza: il pilota
        parla con Gemini per conto suo, e deve poterlo fare con la stessa
        chiave e la stessa scaletta di modelli di ripiego gia' scelta.
        """
        p = pilota.Pilota(self.client, self._model_chain(),
                          narratore=NARRATORE, freno=FRENO_PILOTA)
        esito = p.esegui(args.get("obiettivo", ""), args.get("max_passi"))
        # Al modello serve sapere COSA e' successo per raccontarlo al signore.
        return {"status": esito["status"], "esito": esito["esito"],
                "message": esito["message"], "passi_compiuti": esito["passi"],
                "cosa_ho_fatto": esito["diario"]}

    def esegui_strumento(self, name, args):
        """Esegue uno strumento e ne restituisce il risultato grezzo (un dict).

        Unico punto di esecuzione: lo usano sia il ciclo classico qui sotto sia
        la conversazione dal vivo (live.py). Cosi' le due modalita' hanno gli
        stessi strumenti e le stesse protezioni, conferme comprese.
        """
        fn = DISPATCH.get(name)
        t0 = time.perf_counter()
        try:
            if name == "use_computer":
                out = self._pilota(args)
            else:
                out = fn(args) if fn else {"status": "error", "message": "strumento sconosciuto"}
        except Exception as e:
            out = {"status": "error", "message": str(e)}
        print(f"[jarvis][tempi] strumento {name}: {time.perf_counter() - t0:.2f}s")
        return out

    def _run_tool(self, fc):
        """Esegue uno strumento e costruisce la Part di risposta (testo o immagine)."""
        out = self.esegui_strumento(fc.name, dict(fc.args or {}))

        # Lo screenshot torna come immagine vera dentro il risultato, cosi Jarvis
        # la puo guardare invece di riceverne solo una descrizione.
        if isinstance(out, dict) and out.get("image_base64"):
            blob = types.FunctionResponseBlob(
                data=base64.b64decode(out["image_base64"]),
                mime_type=out.get("media_type", "image/png"))
            return types.Part.from_function_response(
                name=fc.name,
                response={"status": "ok", "note": "Schermo catturato: guarda l'immagine."},
                parts=[types.FunctionResponsePart(inline_data=blob)])

        # Gemini vuole un oggetto JSON: i valori non serializzabili vanno normalizzati.
        try:
            json.dumps(out)
            payload = out if isinstance(out, dict) else {"result": out}
        except (TypeError, ValueError):
            payload = {"result": str(out)}
        return types.Part.from_function_response(name=fc.name, response=payload)

    # ---- gestione della cronologia ----------------------------------------
    @staticmethod
    def _is_tool_result(content):
        return any(getattr(p, "function_response", None) for p in (content.parts or []))

    @staticmethod
    def _has_tool_call(content):
        return any(getattr(p, "function_call", None) for p in (content.parts or []))

    def _drop_incomplete_turn(self):
        """Rimuove code che lascerebbero una chiamata a strumento senza risposta."""
        while self.contents and self.contents[-1].role == "model":
            self.contents.pop()

    def _trim(self, keep=30):
        """Accorcia la cronologia SENZA spezzare le coppie chiamata / risposta.

        Tagliare a un indice arbitrario puo lasciare come primo messaggio una
        function_response orfana: l'API rifiuterebbe la richiesta successiva.
        """
        if len(self.contents) <= keep:
            return
        idx = len(self.contents) - keep
        while idx < len(self.contents):
            c = self.contents[idx]
            if c.role == "user" and not self._is_tool_result(c):
                break
            idx += 1
        self.contents = self.contents[idx:] if idx < len(self.contents) else []

    # ---- fallback locale (senza API key) -----------------------------------
    def _local(self, text, st, ac, say):
        t = text.lower().strip()
        st("thinking")
        if re.search(r"(che ore|che ora|l'ora|l ora)", t):
            st("speaking"); say(f"Sono le {datetime.datetime.now():%H:%M}."); return None
        if re.search(r"(stato|sistema|cpu|memoria|\bram\b|disco)", t):
            ac("Controllo il sistema")
            r = tools.system_status()
            st("speaking"); say(r.get("report", "Stato non disponibile.")); return None
        m = re.search(r"\bapri\s+(.+)$", t)
        if m:
            target = m.group(1).strip(" .")
            ac("Apro un'app")
            ok = tools.open_app(target).get("status") == "ok"
            st("speaking")
            say(f"Fatto, ho aperto {target}." if ok else f"Non sono riuscito ad aprire {target}.")
            return None
        st("speaking")
        say("Per ragionare su questo mi serve la chiave di Google AI Studio nel file punto env. "
            "Per ora gestisco comandi diretti: aprire app, lo stato del sistema o l'ora.")
        return None
