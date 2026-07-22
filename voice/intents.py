"""Comandi diretti: risposte immediate senza passare dal modello.

"Alza il volume" o "che ore sono" non hanno bisogno di un cervello da un milione
di token: farli passare da Gemini costa 4-5 secondi di attesa e una richiesta
della quota giornaliera (sul piano gratuito ne bastano venti per modello).

Qui li riconosciamo con delle regole e li eseguiamo sul posto: risposta
istantanea, quota intatta. Tutto cio che NON e riconosciuto con certezza
prosegue verso il modello: nel dubbio non si indovina.
"""
import datetime
import re

import tools
import webtools

_GIORNI = ["lunedi", "martedi", "mercoledi", "giovedi", "venerdi", "sabato", "domenica"]
_MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
         "agosto", "settembre", "ottobre", "novembre", "dicembre"]

# App riconosciute al volo: solo quelle certe, il resto lo decide il modello.
_APP_DIRETTE = {
    "chrome": "chrome", "google chrome": "chrome",
    "firefox": "firefox", "edge": "edge",
    "vs code": "vs code", "vscode": "vs code", "visual studio code": "vs code",
    "blocco note": "blocco note", "notepad": "blocco note",
    "calcolatrice": "calcolatrice", "spotify": "spotify",
    "esplora risorse": "esplora risorse", "esplora file": "esplora risorse",
    "task manager": "task manager", "gestione attivita": "task manager",
    "terminale": "terminale", "powershell": "powershell", "paint": "paint",
    "impostazioni": "impostazioni",
}


def _normalizza(testo):
    t = (testo or "").lower().strip()
    t = t.replace("’", "'")
    return re.sub(r"\s+", " ", t).strip(" .,!?")


def _ora_a_parole(now):
    ore, minuti = now.hour, now.minute
    if minuti == 0:
        return f"Sono le {ore}."
    return f"Sono le {ore} e {minuti:02d}."


# ---- I singoli comandi -----------------------------------------------------
# Ogni gestore ritorna (frase_da_dire, etichetta_azione) oppure None se, guardando
# meglio, il comando non era davvero suo: in quel caso si prosegue verso il modello.

def _ora(_m):
    return _ora_a_parole(datetime.datetime.now()), None


def _data(_m):
    n = datetime.datetime.now()
    return f"Oggi e {_GIORNI[n.weekday()]} {n.day} {_MESI[n.month - 1]} {n.year}.", None


def _volume_su(m):
    quanto = int(m.group("n")) if m.groupdict().get("n") else 20
    tools.set_volume(delta=abs(quanto))
    return "Volume alzato.", "Regolo il volume"


def _volume_giu(m):
    quanto = int(m.group("n")) if m.groupdict().get("n") else 20
    tools.set_volume(delta=-abs(quanto))
    return "Volume abbassato.", "Regolo il volume"


def _volume_a(m):
    livello = max(0, min(100, int(m.group("n"))))
    tools.set_volume(level=livello)
    return f"Volume al {livello} per cento.", "Regolo il volume"


def _muto(_m):
    tools.set_volume(mute=True)
    return "Fatto.", "Silenzio l'audio"


def _pausa(_m):
    tools.media_control("play_pause")
    return "Fatto.", "Controllo la riproduzione"


def _traccia_avanti(_m):
    tools.media_control("next")
    return "Traccia successiva.", "Controllo la riproduzione"


def _traccia_indietro(_m):
    tools.media_control("previous")
    return "Traccia precedente.", "Controllo la riproduzione"


def _apri_app(m):
    nome = _normalizza(m.group("app"))
    chiave = _APP_DIRETTE.get(nome)
    if not chiave:
        return None                     # app sconosciuta: decide il modello
    esito = tools.open_app(chiave)
    if esito.get("status") != "ok":
        return None                     # fallito: che ci provi il modello
    return f"Fatto, ho aperto {nome}.", "Apro un'app"


def _stato_pc(_m):
    r = tools.system_status()
    if r.get("status") != "ok":
        return None
    return r["report"].replace("\n", ". "), "Controllo il sistema"


def _blocca(_m):
    tools.power_control("lock")
    return "Blocco lo schermo.", "Blocco il PC"


def _minimizza(_m):
    tools.window_control("minimize_all")
    return "Fatto.", "Gestisco le finestre"


def _meteo(m):
    citta = _normalizza(m.groupdict().get("citta") or "")
    citta = re.sub(r"^(a|ad|in|nel|nella|di)\s+", "", citta).strip()
    # _normalizza abbassa tutto: rimettiamo l'iniziale maiuscola, va anche letta.
    citta = " ".join(p.capitalize() for p in citta.split())
    r = webtools.weather(citta, days=1)
    if r.get("status") != "ok":
        return None                     # niente rete: ci pensi il modello
    return r["report"], "Controllo il meteo"


# ---- Tabella delle regole --------------------------------------------------
# Ordine importante: le piu specifiche prima.

_REGOLE = [
    (r"^(che|qual e l)\s*(ore sono|ora e|orario)$", _ora),
    (r"^che ora e$", _ora),
    (r"^(che|quale) giorno e( oggi)?$|^che data e( oggi)?$|^data di oggi$", _data),

    (r"^(alza|aumenta) (il )?volume( di (?P<n>\d{1,3}))?$", _volume_su),
    (r"^(abbassa|riduci) (il )?volume( di (?P<n>\d{1,3}))?$", _volume_giu),
    (r"^(metti |imposta )?(il )?volume (a|al) (?P<n>\d{1,3})( per cento)?$", _volume_a),
    (r"^(muto|silenzia|silenzio|zitto|togli l'audio|riattiva l'audio)$", _muto),

    (r"^(pausa|metti in pausa|ferma la musica|riprendi|play|riproduci)$", _pausa),
    (r"^(traccia |canzone |brano )?(successiv[oa]|prossim[oa]|salta|avanti)$", _traccia_avanti),
    (r"^(traccia |canzone |brano )?precedente$|^indietro$", _traccia_indietro),

    (r"^apri (?P<app>.+)$", _apri_app),
    (r"^(come sta il (pc|computer)|stato del (sistema|pc)|come va il (pc|computer))$", _stato_pc),
    (r"^(blocca|blocco) (il )?(pc|computer|schermo)$", _blocca),
    (r"^(minimizza|riduci) tutt[oe]( le finestre)?$", _minimizza),

    (r"^(che tempo fa|com'e il tempo|meteo)(?P<citta> .+)?$", _meteo),
    (r"^(che tempo fa|meteo)\s*(?P<citta>a .+)$", _meteo),
]

_COMPILATE = [(re.compile(p), fn) for p, fn in _REGOLE]


def match(testo):
    """Prova a servire il comando in locale.

    Ritorna (frase, etichetta_azione) se e stato gestito, altrimenti None:
    in quel caso il turno prosegue normalmente verso il modello.
    """
    norm = _normalizza(testo)
    if not norm:
        return None
    for regex, gestore in _COMPILATE:
        m = regex.match(norm)
        if not m:
            continue
        try:
            esito = gestore(m)
        except Exception:
            return None                 # in caso di problemi: al modello
        if esito is not None:
            return esito
    return None
