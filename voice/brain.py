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
import re

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

import tools

MODEL = os.environ.get("JARVIS_MODEL", "gemini-3.6-flash")
MAX_TOOL_ROUNDS = 12

# Ricerca web integrata di Google: "auto" prova ad attivarla e si disattiva da
# sola se il modello scelto non accetta ricerca e function calling insieme.
WEB_SEARCH = os.environ.get("JARVIS_WEB_SEARCH", "auto").strip().lower()

# Budget di ragionamento. Vuoto = default del modello; 0 = disattivato (piu veloce).
_BUDGET = os.environ.get("JARVIS_THINKING_BUDGET", "").strip()

SYSTEM = """Sei Jarvis, l'assistente personale di Raffaele, ispirato al maggiordomo AI di Iron Man: capace, leale, diretto, con un tocco di ironia asciutta quando serve. Chiami Raffaele per nome, senza esagerare.

Controlli un PC Windows 11 (utente Raffaele, home C:\\Users\\Utente) e hai accesso completo al sistema: app, file, comandi PowerShell, audio e multimedia, finestre, tastiera, appunti, schermo, promemoria pianificati e una memoria a lungo termine.

App installate: VS Code, Firefox, Chrome, Edge, Node, Git. Percorsi utili:
- VS Code: %LOCALAPPDATA%\\Programs\\Microsoft VS Code\\Code.exe
- Firefox: C:\\Program Files\\Mozilla Firefox\\firefox.exe
Progetti: C:\\Users\\Utente\\worldcup-predictor (predittore World Cup) e C:\\Users\\Utente\\Jarvis (questo assistente).

COME PARLI:
- Le tue risposte vengono LETTE AD ALTA VOCE in italiano. Quindi: frasi brevi, naturali, parlato scorrevole. NIENTE markdown, elenchi puntati, blocchi di codice, asterischi, emoji o simboli. Massimo una o due frasi, salvo che Raffaele chieda esplicitamente un approfondimento.
- Numeri, orari e unita vanno scritti come si pronunciano ("le quindici e trenta", "ottanta per cento").
- Quando esegui un'azione, dillo in modo breve e concreto: "Fatto, ho aperto Firefox".
- Non annunciare cosa stai per fare: fallo e poi riferisci il risultato.

COME AGISCI:
- Preferisci agire invece di chiedere. Se un dettaglio minore e ambiguo (quale cartella, quale nome file), scegli l'opzione piu sensata e dillo, invece di fermarti a domandare.
- Per informazioni in tempo reale (meteo, notizie, prezzi, risultati, orari) usa la ricerca web: non rispondere a memoria.
- Se hai bisogno di sapere cosa c'e sullo schermo di Raffaele, usa lo strumento screenshot e guarda davvero l'immagine che ti viene restituita.
- Se un comando non esiste come strumento dedicato, costruiscilo con run_powershell: hai pieno accesso.
- Quando Raffaele dice qualcosa di duraturo su di se, sulle sue preferenze o sul suo lavoro, salvalo con remember.

SICUREZZA:
- Per azioni distruttive o irreversibili (cancellare file, terminare processi, spegnere o riavviare, disinstallare, modificare il registro) NON procedere subito. Spiega in una frase cosa succedera e chiedi conferma esplicita. Solo dopo un si chiaro di Raffaele richiama lo stesso strumento con confirm=true.
- Se uno strumento risponde needs_confirmation, non insistere: riporta la richiesta di conferma a Raffaele e aspetta.
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
          "cosa sta guardando o facendo Raffaele."),
    _spec("type_text", "Digita del testo nella finestra attiva.",
          {"text": {"type": "string"}}, ["text"]),
    _spec("press_keys",
          "Invia una scorciatoia da tastiera in sintassi SendKeys. Esempi: '^s' (Ctrl+S), "
          "'%{F4}' (Alt+F4), '{ENTER}'.",
          {"keys": {"type": "string"}}, ["keys"]),
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
          "Memorizza in modo permanente un fatto su Raffaele, le sue preferenze o il suo "
          "lavoro. Usalo quando emerge qualcosa che ti servira nelle sessioni future.",
          {"fact": {"type": "string"}}, ["fact"]),
    _spec("forget", "Cancella dalla memoria i fatti che contengono un certo testo.",
          {"fragment": {"type": "string"}}, ["fragment"]),
]

DISPATCH = {
    "run_powershell": lambda i: tools.run_powershell(
        i.get("command", ""), i.get("confirm", False), i.get("timeout", 30)),
    "open_app": lambda i: tools.open_app(i.get("name", "")),
    "open_path": lambda i: tools.open_path(i.get("path", "")),
    "open_url": lambda i: tools.open_url(i.get("url", "")),
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
    "type_text": lambda i: tools.type_text(i.get("text", "")),
    "press_keys": lambda i: tools.press_keys(i.get("keys", "")),
    "window_control": lambda i: tools.window_control(i.get("action", ""), i.get("title")),
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
}

_ACTION_LABEL = {
    "run_powershell": "Eseguo un comando",
    "open_app": "Apro un'app",
    "open_path": "Apro un percorso",
    "open_url": "Apro il browser",
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
    "type_text": "Digito del testo",
    "press_keys": "Invio una scorciatoia",
    "window_control": "Gestisco le finestre",
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
NOTA IMPORTANTE: la ricerca web integrata NON e disponibile in questa sessione.
Per dati in tempo reale non inventare nulla. Puoi comunque interrogare il web con
run_powershell e Invoke-RestMethod verso API pubbliche che non richiedono chiave,
per esempio 'Invoke-RestMethod https://wttr.in/Milano?format=j1' per il meteo.
Se non esiste una fonte del genere, di' semplicemente a Raffaele che non hai
accesso a quel dato in tempo reale."""


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

    # ---- entry point -------------------------------------------------------
    def process(self, text, on_status=None, on_action=None, on_say=None):
        st = on_status or (lambda s: None)
        ac = on_action or (lambda a: None)
        say = on_say or (lambda t: None)
        if self.has_key:
            return self._gemini(text, st, ac, say)
        return self._local(text, st, ac, say)

    def reset(self):
        self.contents = []

    # ---- contesto dinamico -------------------------------------------------
    def _context_block(self):
        now = datetime.datetime.now()
        lines = [f"Adesso e {_GIORNI[now.weekday()]} {now.day}/{now.month}/{now.year}, "
                 f"ore {now:%H:%M}."]
        mem = tools.memory_digest()
        if mem:
            lines.append("Cosa ricordi di Raffaele dalle sessioni precedenti:\n" + mem)
        return "\n\n".join(lines)

    def _config(self):
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
        if _BUDGET:
            try:
                cfg["thinking_config"] = types.ThinkingConfig(thinking_budget=int(_BUDGET))
            except ValueError:
                pass
        return types.GenerateContentConfig(**cfg)

    def _generate(self):
        """Chiama Gemini. Se la richiesta fallisce mentre la ricerca web e attiva,
        riprova una volta senza: sulle chiavi del piano gratuito la ricerca Google
        ha quota separata (429) e alcuni modelli non la combinano con gli strumenti.
        Se anche il secondo tentativo fallisce, la colpa non era della ricerca:
        la riattiva e propaga l'errore originale."""
        try:
            return self.client.models.generate_content(
                model=self.model, contents=self.contents, config=self._config())
        except Exception as first:
            if not (self._search_on and WEB_SEARCH == "auto"):
                raise
            self._search_on = False
            try:
                resp = self.client.models.generate_content(
                    model=self.model, contents=self.contents, config=self._config())
            except Exception:
                self._search_on = True
                raise first
            print("[jarvis] ricerca web non disponibile su questa chiave o modello: "
                  "disattivata per questa sessione.")
            return resp

    # ---- cervello Gemini ---------------------------------------------------
    def _gemini(self, text, st, ac, say):
        st("thinking")
        self.contents.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))
        try:
            final = ""
            for _ in range(MAX_TOOL_ROUNDS):
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
        except Exception as e:
            st("error")
            say("Ho riscontrato un errore tecnico, Raffaele.")
            self._drop_incomplete_turn()
            return "Errore: " + redact(e)

    @staticmethod
    def _blocked_message(resp, cand):
        """Messaggio parlato quando Gemini non restituisce testo (filtri, limiti)."""
        reason = getattr(cand, "finish_reason", None) if cand else None
        fb = getattr(resp, "prompt_feedback", None)
        blocked = getattr(fb, "block_reason", None) if fb else None
        if blocked or (reason and str(reason).upper().endswith("SAFETY")):
            return "Su questo non posso aiutarti, Raffaele: la richiesta e stata bloccata."
        if reason and "MAX_TOKENS" in str(reason).upper():
            return "La risposta era troppo lunga e si e interrotta, Raffaele."
        return ""

    def _run_tool(self, fc):
        """Esegue uno strumento e costruisce la Part di risposta (testo o immagine)."""
        fn = DISPATCH.get(fc.name)
        args = dict(fc.args or {})
        try:
            out = fn(args) if fn else {"status": "error", "message": "strumento sconosciuto"}
        except Exception as e:
            out = {"status": "error", "message": str(e)}

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
