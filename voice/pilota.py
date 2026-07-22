"""Pilota automatico: Jarvis guarda lo schermo, decide, agisce e ricontrolla.

La differenza con tutto il resto del progetto: gli altri strumenti sanno GIA'
cosa fare ("alza il volume"), qui invece nessuno sa in anticipo dove sara' il
pulsante da premere. Il ciclo e' quello di una persona davanti al computer:
guarda -> decidi la prossima mossa -> falla -> RIGUARDA per vedere com'e'
andata. La verifica visiva a ogni passo e' cio' che permette di accorgersi di
aver sbagliato invece di proseguire alla cieca.

Sicurezza. Questa e' la cosa piu' invasiva del progetto: un programma che
clicca da solo. Le protezioni vengono prima delle funzioni:
  - non puo' compiere azioni irreversibili (acquisti, invii, cancellazioni):
    non sono tra le mosse disponibili, quindi non e' questione di buona volonta'
    del modello;
  - tetto massimo di passi, per non avvitarsi in cicli infiniti che consumano
    quota;
  - "basta" lo ferma all'istante, tra un passo e l'altro;
  - non puo' cliccare dentro la finestra di Jarvis, altrimenti potrebbe pilotare
    se stesso;
  - racconta ad alta voce ogni mossa PRIMA di compierla, cosi' puoi fermarlo.

Questo modulo non importa brain: riceve dall'esterno il client e l'elenco dei
modelli. Cosi' non ci sono importazioni circolari e si puo' collaudare tutto il
ciclo con un modello finto, senza spendere quota ne' toccare il mouse.
"""
import base64
import os
import threading
import time

try:
    from google.genai import types
except ImportError:
    types = None

import tools

MAX_PASSI = int(os.environ.get("JARVIS_PILOTA_MAX_PASSI", "12"))
# Le immagini pesano: tenerle tutte gonfierebbe la richiesta a ogni passo.
# Al modello serve vedere bene ADESSO e ricordare a parole cosa ha gia' fatto.
IMMAGINI_IN_MEMORIA = 2
ATTESA_DOPO_AZIONE = 0.8      # tempo perche' l'interfaccia reagisca
ATTESA_DOPO_APERTURA = 2.5    # aprire un programma e' piu' lento


ISTRUZIONI = """Sei il pilota automatico di J.A.R.V.I.S. e stai usando il
computer del signore al posto suo.

Ricevi una fotografia dello schermo e scegli UNA sola mossa per volta. Dopo
ogni mossa ricevi una nuova fotografia: guardala per capire se e' andata come
volevi, e correggi il tiro se necessario.

COORDINATE. Indichi i punti su una griglia da 0 a 1000 sull'immagine che vedi:
  x = orizzontale, 0 = bordo SINISTRO, 1000 = bordo DESTRO
  y = verticale,   0 = bordo ALTO,     1000 = bordo BASSO
Il centro dello schermo e' quindi x=500, y=500. Mira sempre al CENTRO
dell'elemento che vuoi premere, non al suo bordo.

REGOLE.
- Una mossa per volta, mai piu' d'una.
- Descrivi in "cosa" l'elemento che stai premendo ("il pulsante Salva"): serve
  al signore per seguirti e fermarti se stai sbagliando.
- Se dopo una mossa lo schermo non e' cambiato come ti aspettavi, NON ripeterla
  identica: guarda meglio e prova diversamente.
- Se una finestra copre quello che ti serve, spostati o chiudila.
- La prima fotografia potrebbe mostrare la finestra di Jarvis stesso: e'
  normale. Usa "apri" per portare in primo piano cio' che ti serve davvero,
  invece di cercare icone sul desktop.
- Quando l'obiettivo e' raggiunto chiama subito "fatto".
- Se ti accorgi che non ce la fai, chiama "rinuncia" spiegando perche'. Non e'
  un fallimento: e' molto meglio che continuare a caso.

COSA NON DEVI FARE MAI.
Non compiere azioni irreversibili o che impegnano il signore: acquisti,
pagamenti, invio di messaggi o email, cancellazione di file, disinstallazioni,
modifiche alle impostazioni di sicurezza, accettazione di condizioni
contrattuali. Se l'obiettivo le richiede, arriva fino al passo precedente,
poi chiama "rinuncia" spiegando cosa manca: sara' il signore a dare l'ultimo
click. Non inserire password."""


def _decl(nome, descrizione, proprieta, obbligatori):
    return {"name": nome, "description": descrizione,
            "parameters": {"type": "object", "properties": proprieta,
                           "required": obbligatori}}


_XY = {
    "x": {"type": "integer", "description": "Orizzontale da 0 (sinistra) a 1000 (destra)."},
    "y": {"type": "integer", "description": "Verticale da 0 (alto) a 1000 (basso)."},
    # Deve essere un NOME, non una frase: finisce dentro "Premo ..." e verra'
    # pronunciato. Con una frase intera Jarvis diceva "Premo cliccare sulla
    # sezione Informazioni", che e' sgrammaticato e si capisce a fatica.
    "cosa": {"type": "string",
             "description": "Solo il NOME dell'elemento, 2-5 parole, senza verbi. "
                            "Bene: 'il pulsante Salva', 'la casella Cerca'. "
                            "Male: 'cliccare sul pulsante per salvare'."},
}

MOSSE = [
    _decl("clicca", "Clicca su un punto dello schermo.",
          dict(_XY, tasto={"type": "string", "enum": ["sinistro", "destro"],
                           "description": "Predefinito: sinistro."},
               doppio={"type": "boolean", "description": "Doppio click, per aprire icone."}),
          ["x", "y", "cosa"]),
    _decl("scrivi", "Scrive del testo dove si trova il cursore. Clicca prima nel campo.",
          {"testo": {"type": "string", "description": "Il testo da scrivere."}},
          ["testo"]),
    _decl("premi", "Preme tasti speciali o combinazioni: invio, esc, tab, "
                   "ctrl+s, alt+f4, freccia giu.",
          {"tasti": {"type": "string", "description": "Es. 'invio', 'ctrl+a', 'esc'."}},
          ["tasti"]),
    _decl("scorri", "Scorre la pagina con la rotellina.",
          {"passi": {"type": "integer", "description": "Positivo = su, negativo = giu."},
           "cosa": {"type": "string", "description": "Cosa stai cercando scorrendo."}},
          ["passi"]),
    _decl("trascina", "Trascina da un punto a un altro tenendo premuto.",
          {"x1": {"type": "integer"}, "y1": {"type": "integer"},
           "x2": {"type": "integer"}, "y2": {"type": "integer"},
           "cosa": {"type": "string"}},
          ["x1", "y1", "x2", "y2", "cosa"]),
    _decl("apri", "Apre un programma o un indirizzo web. Il modo piu' affidabile "
                  "per cominciare, invece di cercare icone sul desktop.",
          {"cosa": {"type": "string", "description": "Nome del programma o URL."}},
          ["cosa"]),
    _decl("attendi", "Aspetta che qualcosa finisca di caricare.",
          {"secondi": {"type": "integer", "description": "Al massimo 10."},
           "perche": {"type": "string"}},
          ["secondi", "perche"]),
    _decl("fatto", "L'obiettivo e' raggiunto.",
          {"riassunto": {"type": "string", "description": "Cosa hai concluso, in una frase."}},
          ["riassunto"]),
    _decl("rinuncia", "Non e' possibile proseguire.",
          {"motivo": {"type": "string", "description": "Perche' ti fermi."}},
          ["motivo"]),
]

_TASTI_FINALI = ("fatto", "rinuncia")


class Fermato(Exception):
    """Il signore ha detto basta."""


class Pilota:
    def __init__(self, client, modelli, narratore=None, freno=None):
        self.client = client
        self.modelli = list(modelli) if modelli else []
        # Racconta ad alta voce; se non lo si collega, lavora in silenzio.
        self.narratore = narratore or (lambda t: None)
        # Evento condiviso: chi vuole fermare il pilota lo alza.
        self.freno = freno or threading.Event()
        self.diario = []
        self._hud = None
        self._hud_visto_il = 0.0

    # ---- protezioni --------------------------------------------------------
    def _controlla_freno(self):
        if self.freno.is_set():
            raise Fermato()

    def _rect_hud(self):
        """Rettangolo della finestra di Jarvis, da non toccare mai.

        Senza questo il pilota potrebbe cliccare nella HUD e comandare se
        stesso: un anello di retroazione che non si spezza da solo.

        Tenuto in cache per qualche secondo perche' cercarlo costa mezzo
        secondo di PowerShell e va controllato prima di OGNI click.
        """
        adesso = time.time()
        if adesso - self._hud_visto_il < 10:
            return self._hud
        self._hud_visto_il = adesso
        r = tools.rect_finestra("J.A.R.V.I.S")
        self._hud = r if r.get("status") == "ok" else None
        return self._hud

    def _vietato(self, x, y):
        hud = self._rect_hud()
        if hud and hud["left"] <= x < hud["right"] and hud["top"] <= y < hud["bottom"]:
            return "quel punto e' dentro la finestra di Jarvis: non ci clicco"
        return None

    # ---- dialogo con il modello -------------------------------------------
    def _config(self):
        return types.GenerateContentConfig(
            system_instruction=ISTRUZIONI,
            tools=[types.Tool(function_declarations=MOSSE)],
            max_output_tokens=1200,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            # Il pilota deve SCEGLIERE una mossa, non chiacchierare.
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="ANY")),
        )

    def _genera(self, contents):
        ultimo = None
        for modello in self.modelli:
            try:
                return self.client.models.generate_content(
                    model=modello, contents=contents, config=self._config())
            except Exception as e:
                ultimo = e
                continue
        raise RuntimeError("nessun modello disponibile: %s" % ultimo)

    @staticmethod
    def _mossa(resp):
        """Estrae la mossa scelta. None se il modello non ne ha scelta alcuna."""
        for cand in (getattr(resp, "candidates", None) or []):
            contenuto = getattr(cand, "content", None)
            for parte in (getattr(contenuto, "parts", None) or []):
                fc = getattr(parte, "function_call", None)
                if fc and fc.name:
                    return fc.name, dict(fc.args or {})
        return None, {}

    @staticmethod
    def _parte_immagine(cattura):
        return types.Part(inline_data=types.Blob(
            mime_type="image/png", data=base64.b64decode(cattura["image_base64"])))

    def _sfoltisci(self, contents):
        """Tiene solo le ultime immagini: le vecchie pesano e non servono piu'.

        Il modello deve vedere bene ADESSO; di cio' che ha gia' fatto gli basta
        il racconto a parole, che resta nella cronologia. Senza questa potatura
        ogni passo trascinerebbe tutte le schermate precedenti e la richiesta
        crescerebbe fino a diventare lenta e costosa.
        """
        viste = 0
        for contenuto in reversed(contents):
            parti = getattr(contenuto, "parts", None) or []
            nuove = []
            for parte in parti:
                if getattr(parte, "inline_data", None) is not None:
                    viste += 1
                    if viste > IMMAGINI_IN_MEMORIA:
                        nuove.append(types.Part.from_text(
                            text="[schermata precedente, non piu' allegata]"))
                        continue
                nuove.append(parte)
            contenuto.parts = nuove

    # ---- esecuzione delle mosse -------------------------------------------
    def _esegui(self, nome, arg, cattura):
        """Compie una mossa. Ritorna (testo_per_il_modello, serve_attesa_lunga)."""
        if nome == "clicca":
            x, y = tools.punto_schermo(arg.get("x", 500), arg.get("y", 500), cattura)
            vietato = self._vietato(x, y)
            if vietato:
                return "Rifiutato: " + vietato, False
            r = tools.mouse_click(x, y,
                                  "right" if arg.get("tasto") == "destro" else "left",
                                  bool(arg.get("doppio")))
            return ("Click eseguito." if r.get("status") == "ok"
                    else "Click fallito: " + r.get("message", "")), False

        if nome == "scrivi":
            testo = str(arg.get("testo", ""))
            r = tools.type_text(testo)
            return ("Testo scritto." if r.get("status") == "ok"
                    else "Scrittura fallita: " + r.get("message", "")), False

        if nome == "premi":
            r = tools.press_keys(str(arg.get("tasti", "")))
            return ("Tasti premuti." if r.get("status") == "ok"
                    else "Pressione fallita: " + r.get("message", "")), False

        if nome == "scorri":
            r = tools.mouse_scroll(arg.get("passi", -3))
            return ("Scorrimento eseguito." if r.get("status") == "ok"
                    else "Scorrimento fallito: " + r.get("message", "")), False

        if nome == "trascina":
            x1, y1 = tools.punto_schermo(arg.get("x1", 0), arg.get("y1", 0), cattura)
            x2, y2 = tools.punto_schermo(arg.get("x2", 0), arg.get("y2", 0), cattura)
            for px, py in ((x1, y1), (x2, y2)):
                vietato = self._vietato(px, py)
                if vietato:
                    return "Rifiutato: " + vietato, False
            r = tools.mouse_drag(x1, y1, x2, y2)
            return ("Trascinamento eseguito." if r.get("status") == "ok"
                    else "Trascinamento fallito: " + r.get("message", "")), False

        if nome == "apri":
            cosa = str(arg.get("cosa", "")).strip()
            r = (tools.open_url(cosa) if cosa.lower().startswith(("http://", "https://", "www."))
                 else tools.open_app(cosa))
            return ("Aperto." if r.get("status") == "ok"
                    else "Apertura fallita: " + r.get("message", "")), True

        if nome == "attendi":
            try:
                s = max(1, min(10, int(arg.get("secondi", 2))))
            except (TypeError, ValueError):
                s = 2
            # A pezzetti, cosi' "basta" ferma anche durante l'attesa.
            for _ in range(s * 4):
                self._controlla_freno()
                time.sleep(0.25)
            return "Attesa di %d secondi conclusa." % s, False

        return "Mossa '%s' sconosciuta." % nome, False

    @staticmethod
    def _racconto(nome, arg):
        """La frase che Jarvis pronuncia PRIMA di agire."""
        cosa = str(arg.get("cosa", "")).strip()
        if nome == "clicca":
            return ("Premo " + cosa) if cosa else "Clicco."
        if nome == "scrivi":
            t = str(arg.get("testo", ""))
            return "Scrivo: " + (t[:60] + "…" if len(t) > 60 else t)
        if nome == "premi":
            # Non "Premo": quel verbo e' gia' del click, e sentirli uguali
            # rende impossibile capire a voce cosa stia facendo.
            return "Uso i tasti " + str(arg.get("tasti", ""))
        if nome == "scorri":
            giu = int(arg.get("passi", -3) or -3) < 0
            return "Scorro in basso" if giu else "Scorro in alto"
        if nome == "trascina":
            return ("Trascino " + cosa) if cosa else "Trascino."
        if nome == "apri":
            return "Apro " + cosa
        if nome == "attendi":
            return "Attendo: " + str(arg.get("perche", "caricamento"))
        return nome

    # ---- ciclo principale --------------------------------------------------
    def esegui(self, obiettivo, max_passi=None):
        # Ogni uscita passa da _esito(): un chiamante non deve mai ricevere un
        # dizionario di forma diversa a seconda di DOVE si e' fermato.
        self.diario = []
        if types is None:
            return self._esito("error", "Libreria Gemini non disponibile.")
        if not self.client:
            return self._esito("error", "Serve la chiave di Google AI Studio.")
        obiettivo = (obiettivo or "").strip()
        if not obiettivo:
            return self._esito("error", "Obiettivo mancante.")

        limite = max(1, min(30, int(max_passi or MAX_PASSI)))
        self.freno.clear()

        cattura = tools._cattura(monitor="attivo")
        if cattura.get("status") != "ok":
            return self._esito("error", "Non riesco a vedere lo schermo: "
                               + cattura.get("message", ""))

        contents = [types.Content(role="user", parts=[
            types.Part.from_text(text=(
                "Obiettivo: %s\n\n"
                "Questa e' la fotografia dello schermo attivo (%d x %d pixel veri). "
                "Scegli la prima mossa." % (obiettivo, cattura["larghezza"], cattura["altezza"]))),
            self._parte_immagine(cattura),
        ])]

        try:
            for passo in range(1, limite + 1):
                self._controlla_freno()
                resp = self._genera(contents)
                nome, arg = self._mossa(resp)
                if not nome:
                    return self._esito("error", "Il pilota non ha saputo scegliere una mossa.")

                if nome in _TASTI_FINALI:
                    testo = str(arg.get("riassunto") or arg.get("motivo") or "")
                    self.diario.append(nome + ": " + testo)
                    return self._esito("ok" if nome == "fatto" else "rinuncia", testo, passo)

                racconto = self._racconto(nome, arg)
                self.narratore(racconto)
                self.diario.append(racconto)

                self._controlla_freno()
                esito, lenta = self._esegui(nome, arg, cattura)
                time.sleep(ATTESA_DOPO_APERTURA if lenta else ATTESA_DOPO_AZIONE)

                self._controlla_freno()
                cattura = tools._cattura(monitor="attivo")
                if cattura.get("status") != "ok":
                    return self._esito("error", "Ho perso di vista lo schermo.")

                # La risposta alla mossa E' la nuova schermata: il modello vede
                # con i propri occhi cosa ha combinato, invece di fidarsi.
                contents.append(resp.candidates[0].content)
                contents.append(types.Content(role="user", parts=[
                    types.Part.from_text(text="Esito: %s  (passo %d di %d)" % (esito, passo, limite)),
                    self._parte_immagine(cattura),
                ]))
                self._sfoltisci(contents)

            return self._esito("limite", "Ho raggiunto il limite di %d passi." % limite, limite)
        except Fermato:
            return self._esito("fermato", "Interrotto su sua richiesta.")
        except Exception as e:
            return self._esito("error", str(e))

    def _esito(self, stato, messaggio, passi=None):
        return {"status": "ok" if stato == "ok" else "error", "esito": stato,
                "message": messaggio, "passi": passi if passi is not None else len(self.diario),
                "diario": list(self.diario)}
