"""Riepilogo della giornata, pronunciato da Jarvis.

Mette insieme meteo, promemoria, to-do e stato del PC in un unico discorso.
Non passa dal modello: le fonti sono gia' strutturate e la frase la componiamo
qui, quindi costa zero richieste e risponde subito.

Ogni pezzo e' facoltativo: se una fonte non risponde (rete assente, promemoria
non leggibili) il briefing prosegue senza quel pezzo invece di fallire.
"""
import datetime
import os
import re

import tools
import webtools

_GIORNI = ["lunedi", "martedi", "mercoledi", "giovedi", "venerdi", "sabato", "domenica"]
_MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
         "agosto", "settembre", "ottobre", "novembre", "dicembre"]

STATO = os.path.join(tools.ROOT, ".ultimo-briefing")


def _saluto(ora):
    if ora < 5:
        return "Buonanotte"
    if ora < 13:
        return "Buongiorno"
    if ora < 18:
        return "Buon pomeriggio"
    return "Buonasera"


def _data_a_parole(adesso):
    return (f"{_GIORNI[adesso.weekday()]} {adesso.day} {_MESI[adesso.month - 1]}, "
            f"sono le {adesso.hour} e {adesso.minute:02d}")


def _meteo(citta):
    try:
        r = webtools.weather(citta or "", days=1)
        return r.get("report") if r.get("status") == "ok" else None
    except Exception:
        return None


def _promemoria_di_oggi():
    """Promemoria pianificati che scadono entro la giornata."""
    try:
        r = tools.list_reminders()
        if r.get("status") != "ok":
            return None
        oggi = datetime.date.today()
        prossimi = []
        for riga in r.get("reminders", []):
            # formato: "Jarvis_xxxx -> 22/07/2026 15:30:00"
            m = re.search(r"->\s*(\d{1,2})[/-](\d{1,2})[/-](\d{4})\s+(\d{1,2}):(\d{2})", riga)
            if not m:
                continue
            g, mese, anno, oh, om = (int(x) for x in m.groups())
            if datetime.date(anno, mese, g) == oggi:
                prossimi.append(f"{oh} e {om:02d}")
        if not prossimi:
            return None
        if len(prossimi) == 1:
            return f"Ha un promemoria oggi alle {prossimi[0]}."
        return f"Ha {len(prossimi)} promemoria oggi, il primo alle {prossimi[0]}."
    except Exception:
        return None


def _todo_aperti():
    try:
        r = tools.list_todos()
        aperti = [t for t in r.get("todos", []) if t.startswith("- [ ]")]
        if not aperti:
            return None
        if len(aperti) == 1:
            return "Ha una cosa da fare in sospeso: " + aperti[0][6:].strip() + "."
        return f"Ha {len(aperti)} cose da fare in sospeso."
    except Exception:
        return None


def _sistema():
    try:
        s = tools.system_status_brief()
        pezzi = []
        libero = s.get("diskFree")
        if isinstance(libero, (int, float)) and libero >= 0:
            if libero < 20:
                pezzi.append(f"Attenzione, sul disco restano solo {int(libero)} gigabyte.")
            else:
                pezzi.append(f"Sul disco ci sono {int(libero)} gigabyte liberi.")
        bat = s.get("battery", -1)
        if isinstance(bat, int) and 0 <= bat <= 25:
            pezzi.append(f"La batteria e' al {bat} per cento.")
        return " ".join(pezzi) or None
    except Exception:
        return None


def componi(citta=""):
    """Costruisce il testo del briefing. Ritorna una stringa pronta da leggere."""
    adesso = datetime.datetime.now()
    parti = [f"{_saluto(adesso.hour)}, signore. Oggi e' {_data_a_parole(adesso)}."]
    for pezzo in (_meteo(citta), _promemoria_di_oggi(), _todo_aperti(), _sistema()):
        if pezzo:
            parti.append(pezzo)
    if len(parti) == 1:
        parti.append("Non ho altro da segnalare.")
    return " ".join(parti)


# ---- briefing automatico: una sola volta al giorno -------------------------

def gia_fatto_oggi():
    try:
        with open(STATO, "r", encoding="utf-8") as f:
            return f.read().strip() == datetime.date.today().isoformat()
    except OSError:
        return False


def segna_fatto():
    try:
        with open(STATO, "w", encoding="utf-8") as f:
            f.write(datetime.date.today().isoformat())
    except OSError:
        pass
