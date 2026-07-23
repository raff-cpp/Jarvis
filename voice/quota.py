"""Traccia l'uso giornaliero dei modelli Gemini: quota residua e latenza.

Serve a due cose:
- mostrare sulla HUD quanta quota gratuita resta per ogni modello (il piano
  gratuito di AI Studio limita le richieste PER MODELLO e PER GIORNO);
- misurare quale modello risponde e quanto ci mette (strumentazione dei tempi),
  cosi "e diventato piu lento" smette di essere una sensazione e diventa un dato.

I contatori stanno in un file JSON accanto agli altri dati di Jarvis e si
azzerano a ogni cambio di giornata. I limiti giornalieri del piano gratuito non
sono pubblicati con precisione e cambiano nel tempo: la percentuale residua e'
quindi una STIMA rispetto a un tetto configurabile (JARVIS_QUOTA_GIORNALIERA).
Un 429, invece, e' un fatto certo: inchioda quel modello a zero per la giornata.
"""
import datetime
import json
import os
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FILE = os.path.join(ROOT, "quota-modelli.json")

# Tetto giornaliero stimato di richieste per modello (piano gratuito). Non e'
# pubblicato con esattezza: e' un riferimento per calcolare la percentuale, e si
# regola dal .env senza toccare il codice. Un valore per modello ha la
# precedenza (JARVIS_QUOTA_<MODELLO>), altrimenti vale quello generale.
_DEFAULT_BUDGET = int(os.environ.get("JARVIS_QUOTA_GIORNALIERA", "50") or "50")

_lock = threading.Lock()
_state = None            # {"date": "AAAA-MM-GG", "models": {nome: {...}}}

# Etichette compatte e leggibili per la HUD: "gemini-3.6-flash" -> "Gemini 3.6".
_PREFISSO = "gemini-"


def _oggi():
    return datetime.date.today().isoformat()


def _vuoto():
    return {"date": _oggi(), "models": {}}


def _carica():
    global _state
    if _state is not None and _state.get("date") == _oggi():
        return _state
    # File assente, illeggibile, o di ieri: si riparte puliti per la giornata.
    caricato = None
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            caricato = json.load(f)
    except (OSError, json.JSONDecodeError):
        caricato = None
    if not isinstance(caricato, dict) or caricato.get("date") != _oggi():
        caricato = _vuoto()
    _state = caricato
    return _state


def _salva():
    try:
        with open(_FILE, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _riga(models, nome):
    r = models.get(nome)
    if r is None:
        r = {"ok": 0, "fail": 0, "exhausted": False, "last_latency": None,
             "sum_latency": 0.0}
        models[nome] = r
    return r


def budget(model):
    """Tetto giornaliero per questo modello: override per-modello o generale."""
    chiave = "JARVIS_QUOTA_" + model.upper().replace("-", "_").replace(".", "_")
    val = os.environ.get(chiave)
    if val:
        try:
            return max(1, int(val))
        except ValueError:
            pass
    return max(1, _DEFAULT_BUDGET)


def record_success(model, latency=None):
    """Registra una risposta andata a buon fine (e la sua latenza in secondi)."""
    with _lock:
        st = _carica()
        r = _riga(st["models"], model)
        r["ok"] += 1
        # Ha risposto: non e' esaurito adesso. Se il 429 di prima era un limite al
        # minuto (non giornaliero), cosi il pannello torna a mostrare la quota vera.
        r["exhausted"] = False
        if latency is not None:
            r["last_latency"] = round(float(latency), 2)
            r["sum_latency"] = round(r.get("sum_latency", 0.0) + float(latency), 2)
        _salva()


def record_exhausted(model):
    """Registra un 429: quota esaurita per questo modello, oggi."""
    with _lock:
        st = _carica()
        r = _riga(st["models"], model)
        r["fail"] += 1
        r["exhausted"] = True
        _salva()


def is_exhausted(model):
    with _lock:
        st = _carica()
        r = st["models"].get(model)
        return bool(r and r.get("exhausted"))


def known(model):
    """True se oggi sappiamo gia qualcosa di questo modello (usato o esaurito).

    Serve alla sonda d'avvio per NON sprecare una richiesta quando l'esito e'
    gia noto: se il modello e' stato toccato oggi, il pannello e' gia realistico.
    """
    with _lock:
        st = _carica()
        r = st["models"].get(model)
        return bool(r and (r.get("ok") or r.get("exhausted")))


def etichetta(model):
    """Nome leggibile per la HUD: 'gemini-3.6-flash' -> 'Gemini 3.6 flash'."""
    nome = model
    if nome.startswith(_PREFISSO):
        nome = nome[len(_PREFISSO):]
    nome = nome.replace("-", " ")
    return "Gemini " + nome


def _remaining_pct(riga, tetto):
    if riga.get("exhausted"):
        return 0
    resto = 100.0 * (1.0 - riga.get("ok", 0) / float(tetto))
    return max(0, min(100, int(round(resto))))


def snapshot(chain):
    """Stato dei modelli della catena, nell'ordine dato, per il pannello HUD.

    Ogni voce: nome tecnico, etichetta, quota residua stimata (%), richieste
    riuscite oggi, tetto, se e' esaurito, latenza ultima/media.
    """
    with _lock:
        st = _carica()
        out = []
        visti = set()
        for model in chain:
            if model in visti:
                continue
            visti.add(model)
            riga = st["models"].get(model, {})
            tetto = budget(model)
            ok = riga.get("ok", 0)
            avg = round(riga.get("sum_latency", 0.0) / ok, 2) if ok else None
            out.append({
                "model": model,
                "label": etichetta(model),
                "remaining": _remaining_pct(riga, tetto),
                "used": ok,
                "budget": tetto,
                "exhausted": bool(riga.get("exhausted")),
                "last_latency": riga.get("last_latency"),
                "avg_latency": avg,
            })
        return out
