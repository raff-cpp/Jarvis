"""Strumenti di rete di Jarvis: ricerca web, meteo, lettura pagine.

La ricerca integrata di Google (grounding) ha quota ZERO sulle chiavi gratuite
di AI Studio: risponde 429 anche quando il resto funziona. Qui usiamo servizi
pubblici che non richiedono alcuna chiave, quindi Jarvis sa davvero rispondere
su meteo, notizie e fatti in tempo reale.
"""
import html
import json
import re
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _fetch(url, data=None, timeout=15):
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA,
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.5",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _clean(fragment):
    """Toglie i tag e decodifica le entita HTML."""
    return html.unescape(re.sub(r"<[^>]+>", "", fragment or "")).strip()


def _real_url(href):
    """DuckDuckGo avvolge i link in un redirect: estrae la destinazione vera."""
    if "uddg=" in href:
        try:
            return urllib.parse.unquote(
                urllib.parse.parse_qs(urllib.parse.urlparse(href).query)["uddg"][0])
        except (KeyError, IndexError, ValueError):
            pass
    return href if href.startswith("http") else "https:" + href


def web_search(query, max_results=6):
    """Cerca sul web e restituisce titoli, link e riassunti."""
    if not query:
        return {"status": "error", "message": "Query mancante."}
    try:
        body = _fetch("https://html.duckduckgo.com/html/",
                      data=urllib.parse.urlencode({"q": query, "kl": "it-it"}).encode())
    except Exception as e:
        return {"status": "error", "message": f"Ricerca non riuscita: {e}"}

    links = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', body, re.S)

    results = []
    for i, (href, title) in enumerate(links[:int(max_results)]):
        results.append({
            "title": _clean(title),
            "url": _real_url(href),
            "snippet": _clean(snippets[i]) if i < len(snippets) else "",
        })
    if not results:
        return {"status": "error",
                "message": "Nessun risultato (il motore potrebbe aver cambiato formato)."}
    return {"status": "ok", "query": query, "count": len(results), "results": results}


# Descrizioni meteo: wttr.in le restituisce in inglese, qui il parlato e italiano.
_METEO = {
    "sunny": "sereno", "clear": "sereno", "partly cloudy": "parzialmente nuvoloso",
    "cloudy": "nuvoloso", "overcast": "coperto", "mist": "foschia", "fog": "nebbia",
    "freezing fog": "nebbia gelata", "patchy rain nearby": "qualche pioggia nei dintorni",
    "patchy rain possible": "possibili piogge", "light rain": "pioggia leggera",
    "moderate rain": "pioggia moderata", "heavy rain": "pioggia intensa",
    "light drizzle": "pioggerella", "patchy light drizzle": "pioggerella a tratti",
    "light rain shower": "rovescio leggero", "moderate or heavy rain shower": "rovescio intenso",
    "thundery outbreaks possible": "possibili temporali",
    "patchy light rain with thunder": "pioggia e tuoni",
    "moderate or heavy rain with thunder": "temporale",
    "light snow": "neve leggera", "moderate snow": "neve", "heavy snow": "neve intensa",
    "patchy snow possible": "possibile neve", "blizzard": "bufera",
}


def _desc_it(testo):
    return _METEO.get((testo or "").strip().lower(), (testo or "").strip().lower())


def weather(location="", days=1):
    """Meteo attuale e previsione. Senza citta usa la posizione da IP."""
    place = urllib.parse.quote((location or "").strip())
    try:
        raw = _fetch(f"https://wttr.in/{place}?format=j1", timeout=20)
        data = json.loads(raw)
    except Exception as e:
        return {"status": "error", "message": f"Meteo non disponibile: {e}"}

    try:
        cur = data["current_condition"][0]
        area = (data.get("nearest_area") or [{}])[0]
        # Il nome chiesto dal signore e in italiano; quello dell'API e in inglese
        # ("Milan"), quindi preferiamo il suo quando l'ha indicato lui.
        citta = (location.strip() if location and location.strip()
                 else ((area.get("areaName") or [{}])[0].get("value") or "la tua posizione"))
        righe = [f"A {citta} ci sono {cur['temp_C']} gradi, "
                 f"{_desc_it(cur['weatherDesc'][0]['value'])}, "
                 f"percepiti {cur['FeelsLikeC']}, umidita {cur['humidity']} per cento."]
        etichette = ["Oggi", "Domani", "Dopodomani"]
        for i, giorno in enumerate(data.get("weather", [])[:max(0, int(days))]):
            quando = etichette[i] if i < len(etichette) else f"fra {i} giorni"
            righe.append(f"{quando} minima {giorno['mintempC']} e massima "
                         f"{giorno['maxtempC']} gradi.")
        return {"status": "ok", "location": citta, "report": " ".join(righe)}
    except (KeyError, IndexError, TypeError) as e:
        return {"status": "error", "message": f"Risposta meteo inattesa: {e}"}


def fetch_page(url, max_chars=5000):
    """Scarica una pagina e ne restituisce il testo leggibile."""
    if not url:
        return {"status": "error", "message": "URL mancante."}
    if not re.match(r"^https?://", url):
        url = "https://" + url
    try:
        body = _fetch(url, timeout=20)
    except Exception as e:
        return {"status": "error", "message": f"Pagina non raggiungibile: {e}"}

    titolo = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    # Via script, stili e markup: resta il testo.
    testo = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", body)
    testo = re.sub(r"(?is)<(br|/p|/div|/li|/h[1-6])>", "\n", testo)
    testo = _clean(testo)
    testo = re.sub(r"[ \t]+", " ", testo)
    testo = re.sub(r"\n\s*\n+", "\n\n", testo).strip()
    return {"status": "ok", "url": url,
            "title": _clean(titolo.group(1)) if titolo else "",
            "truncated": len(testo) > int(max_chars),
            "text": testo[:int(max_chars)]}
