# J.A.R.V.I.S. — assistente personale di Raffaele

Assistente vocale in stile Iron Man che gira **in locale** sul PC: interfaccia HUD nel
browser, riconoscimento vocale, sintesi vocale italiana e un cervello **Gemini** con
**accesso completo al sistema** (app, file, PowerShell, audio, finestre, schermo).

## Avvio rapido

1. Copia `voice\.env.example` in `voice\.env` e inserisci la tua chiave:

   ```
   GEMINI_API_KEY=AIza...
   ```

   La chiave si crea gratis su <https://aistudio.google.com/apikey>.

2. Doppio click su **`AVVIA-JARVIS.cmd`** (al primo avvio prepara da solo l'ambiente
   Python e installa le dipendenze).

   Da terminale, in alternativa: `jarvis voce`

3. Si apre la HUD. Clicca il microfono una volta per concedere il permesso, poi di'
   **«Jarvis»** seguito da quello che vuoi.

## Come si parla con lui

| Modo | Come |
|---|---|
| **Scorciatoia globale** | **Ctrl+Alt+J** da qualunque applicazione: la HUD viene in primo piano e il microfono si apre già armato. |
| **Push-to-talk** | Clicca il microfono e parla. |
| **Sempre in ascolto** | Attiva *Ascolto continuo*: risponde solo dopo la parola «Jarvis». |
| **Senza parola chiave** | Ascolto continuo + togli *Parola d'attivazione*: esegue qualunque frase. |
| **Testo** | Scrivi nel campo in basso e premi Invio. |

Puoi dire tutto in una frase (*«Jarvis, apri Firefox»*) oppure solo *«Jarvis»*, aspettare
che il reattore si illumini e poi dare il comando.

**Per interromperlo mentre parla** di' *«basta»* (o stop, fermati, zitto). Funziona
solo in **ascolto continuo**, e solo se la parola è pronunciata da sola: mentre
parla nessun'altra frase viene interpretata, così una parola captata per sbaglio
può al massimo zittirlo — mai far partire un comando.

La scorciatoia **Ctrl+Alt+J** si cambia con `JARVIS_HOTKEY` nel file `.env`. Se un
altro programma la occupa già, Jarvis lo scrive all'avvio e parte lo stesso.

Esempi: *«che ore sono»*, *«com'e messo il PC»*, *«apri VS Code»*, *«alza il volume»*,
*«metti in pausa la musica»*, *«guarda lo schermo e dimmi cosa vedi»*, *«cerca i file
che si chiamano fattura»*, *«ricordami di chiamare Marco domani alle nove»*,
*«che tempo fa a Milano»*, *«ricordati che preferisco Python»*.

## Cosa sa fare

- **Sistema** — stato di CPU/RAM/dischi/batteria/rete, processi attivi, terminare
  processi, bloccare / sospendere / riavviare / spegnere il PC.
- **App e file** — aprire app e siti, elencare e cercare file ovunque, leggere e
  scrivere file, aprire cartelle.
- **PowerShell** — qualunque comando, quindi in pratica qualunque cosa faccia Windows.
- **Audio** — volume (assoluto o relativo), muto, play/pausa, brano successivo e precedente.
- **Schermo e input** — cattura lo schermo e lo **guarda davvero** (vision), digita
  testo, invia scorciatoie da tastiera, gestisce e mette a fuoco le finestre, appunti.
- **Promemoria** — programma avvisi a un orario preciso tramite le Attività pianificate
  di Windows: suonano anche se Jarvis è chiuso o il PC è stato riavviato.
- **Web** — ricerca in tempo reale per meteo, notizie, prezzi, risultati.
- **Memoria** — ricorda fatti e preferenze fra una sessione e l'altra (`memoria.json`).
- **Note e to-do** — le stesse liste markdown usate dai comandi da terminale.

## Sicurezza

- **La chiave API non arriva mai al browser.** Vive solo in `voice\.env`, letta dal
  processo Python. La pagina web riceve unicamente `has_key: true/false` e il nome del
  modello; ogni chiamata a Gemini parte dal server.
- **`.env` è in `.gitignore`** e non finisce nei commit.
- Il server ascolta **solo su `127.0.0.1`**: non è raggiungibile dalla rete locale.
- Le azioni **distruttive** (cancellare file, terminare processi, spegnere, riavviare,
  disinstallare, toccare il registro, sovrascrivere file esistenti) sono bloccate al
  primo tentativo: Jarvis deve spiegarti cosa succederà e ottenere il tuo «sì» prima di
  poterle eseguire.

Jarvis ha di proposito pieno accesso al PC: è il senso del progetto. Trattalo come un
terminale amministratore — non lasciarlo in ascolto continuo davanti a estranei.

## Comandi da terminale

```
jarvis voce                 Avvia J.A.R.V.I.S. vocale
jarvis stato                Stato sistema (CPU, RAM, disco)
jarvis giornata             Apre le app di lavoro
jarvis todo ["testo"]       Mostra o aggiunge un todo
jarvis nota ["testo"]       Mostra o salva una nota
jarvis promemoria ["testo"] Mostra o aggiunge un promemoria
```

Per usarlo da qualsiasi cartella, aggiungi `C:\Users\Utente\Jarvis` al PATH.

## Struttura

```
Jarvis/
  AVVIA-JARVIS.cmd     Avvio con doppio click
  jarvis.cmd           Entry point da terminale
  memoria.json         Memoria a lungo termine (non committata)
  todo.md  note.md  promemoria.md
  scripts/             Dispatcher e script PowerShell
  voice/
    app.py             Server Flask + WebSocket
    brain.py           Cervello Gemini, definizione dei tool
    tools.py           Implementazione degli strumenti di sistema
    modelli.py         Elenca i modelli Gemini disponibili sulla tua chiave
    .env               Chiave API (NON committato — crealo tu)
    .env.example       Modello da copiare
    web/               HUD: index.html, style.css, app.js
```

## Configurazione

In `voice\.env`:

| Variabile | Default | A cosa serve |
|---|---|---|
| `GEMINI_API_KEY` | — | Obbligatoria per il cervello AI (va bene anche `GOOGLE_API_KEY`) |
| `JARVIS_MODEL` | `gemini-3.6-flash` | Modello Gemini. I `flash` sono i più adatti alla voce (rispondono in 4-5 s) |
| `JARVIS_WEB_SEARCH` | `auto` | Ricerca web Google: `auto` la disattiva da sola se la chiave o il modello non la supportano |
| `JARVIS_THINKING_BUDGET` | vuoto | Token di ragionamento. `0` = disattivato, risposte più rapide |
| `JARVIS_PORT` | `8730` | Porta del server locale |

Per vedere quali modelli sono disponibili sulla tua chiave:

```
cd voice
..\.venv\Scripts\python.exe modelli.py
```

> **Attenzione ai nomi dei modelli.** L'elenco restituito da Google include anche
> modelli *ritirati*, che rispondono `404 no longer available to new users` appena
> li usi (è il caso di `gemini-2.5-flash`). Se Jarvis dà 404, cambia `JARVIS_MODEL`.

### Ricerca web

La ricerca *integrata* di Google ha **quota zero** sulle chiavi gratuite (risponde
`429` anche quando tutto il resto funziona). Jarvis quindi non la usa: ha strumenti
web propri che non richiedono né chiave né quota.

| Strumento | Fonte | A cosa serve |
|---|---|---|
| `web_search` | DuckDuckGo | notizie, risultati, prezzi, fatti recenti |
| `weather` | wttr.in | meteo attuale e previsioni |
| `fetch_page` | diretta | leggere o riassumere una pagina |

Funzionano subito, senza configurazione. Se `JARVIS_WEB_SEARCH=auto` e il grounding
Google fosse disponibile, Jarvis lo userebbe in aggiunta; con `off` (consigliato)
evita la chiamata sprecata.

## Controllo a gesti (webcam)

Attiva **GESTI** nel pannello di sinistra: compare l'anteprima della webcam con
lo scheletro della mano. Il riconoscimento gira **nel browser** (MediaPipe), non
sul server.

Niente scatta finché non **armi** il sistema: tieni il **palmo aperto** verso la
webcam per mezzo secondo. La cornice si illumina. Dopo 4 secondi senza mano si
disarma da solo.

| Gesto | Effetto |
|---|---|
| **Pinch** pollice-indice (altre dita chiuse) | volume, come una manopola |
| **Mano aperta**, swipe ← / → | traccia precedente / successiva |
| **Chiudi la mano** a pugno e tienila | finestra sull'altro schermo |

Lo spostamento finestra **non è più uno swipe**: chiudere la mano è una
transizione di stato, non un movimento, quindi non soffre della sfocatura che
rendeva instabile il gesto precedente. E con due schermi non serve indicare un
verso — «l'altro» è univoco. Per rifarlo, riapri la mano e richiudila.

Le stesse azioni restano disponibili a voce (*«sposta la finestra a destra»*).

Perché è tarato "stretto": le mani si muovono in continuazione mentre si parla,
quindi l'armamento esplicito e le soglie ampie evitano che un gesto casuale salti
una traccia. Se un gesto non scatta, rifallo più ampio.

**Il volume si tara da solo.** La massima apertura comoda delle dita cambia da
persona a persona e con la distanza dalla webcam, quindi la scala si adatta ai
valori che vede: dopo una prima apertura completa, il 100% è sempre raggiungibile.

**Gli swipe si misurano in lunghezze di palmo**, non in frazioni di inquadratura:
lo stesso movimento vale uguale da vicino e da lontano. Servono circa **un palmo**
di corsa, e il riconoscimento tollera che la posa sfarfalli su qualche fotogramma
(la mano in movimento è sfocata: pretendere la posa perfetta su *ogni* fotogramma
faceva scartare quasi ogni gesto).

**Pugno e pinch non si confondono più.** Non sono distinti da una soglia secca —
che con la mano in movimento oscillava, facendo partire il volume durante uno
swipe a pugno chiuso — ma da quanto indice e pollice *sporgono oltre il medio*:
quasi zero nel pugno, netta nel pinch. Il pinch inoltre si aggancia solo a mano
ferma e dopo un decimo di secondo, perché è l'unico gesto con effetto continuo.

**Numeri di taratura** sull'anteprima:

| Valore | Cosa dice |
|---|---|
| `pinch` | apertura attuale delle dita, e la scala tarata sulla tua mano |
| `sporg` | separa pugno e pinch: sotto `0.12` pugno, sopra `0.25` pinch |
| `dita` | quante risultano distese (ne bastano 3 per armare) |
| `swipe` | corsa attuale in palmi, rispetto a quella richiesta |

**Clicca sull'anteprima** per nasconderli quando non ti servono più.

> La webcam resta accesa finché l'interruttore è attivo: spegnilo quando non
> serve, anche perché occupa la camera e le videochiamate non la troverebbero
> libera.

## Jarvis parla per primo

Non aspetta sempre di essere interrogato. Sorveglia alcune cose e ti avvisa a
voce:

- **download completato** — controlla la cartella Download e ignora i file
  ancora in corso (`.crdownload`, `.part`…), così non annuncia scaricamenti a metà
- **batteria sotto il 15%** — una volta sola; si riarma solo se risale sopra il 25%
- **promemoria pronunciati da lui**, con la sua voce, invece del riquadro
  anonimo di Windows. Se la finestra è chiusa ripiega sulla notifica di sistema,
  così il promemoria non va mai perso

Gli annunci **non interrompono mai**: se Jarvis sta rispondendo o sta elaborando
una richiesta, l'avviso aspetta il momento buono e poi esce, in ordine.

Le condizioni sono regole locali e le frasi sono fisse: **nessun consumo di quota**.
Si disattiva con `JARVIS_PROATTIVO=off`.

## Il riepilogo della giornata

Di' **«buongiorno»** (o *briefing*, *riepilogo*, *fammi il punto*) e Jarvis mette
insieme meteo, promemoria di oggi, cose da fare in sospeso e spazio su disco.

> *«Buonasera, signore. Oggi è mercoledì 22 luglio, sono le 19 e 51. A Cesa ci
> sono 32 gradi… Ha 2 cose da fare in sospeso. Sul disco ci sono 38 gigabyte
> liberi.»*

Arriva **da solo al primo avvio di ogni giornata**, non appena la HUD è pronta.
Non passa dal modello: costa **zero richieste** e risponde in un paio di secondi.
Ogni pezzo è facoltativo — se il meteo non risponde, il resto del riepilogo
arriva lo stesso.

Disattivabile con `JARVIS_BRIEFING_AUTO=off`; la città per il meteo si fissa con
`JARVIS_CITTA`.

## Comandi istantanei

I comandi semplici non passano dall'AI: vengono riconosciuti ed eseguiti in locale,
quindi rispondono **subito** e non consumano quota.

```
che ore sono          che giorno è          come sta il PC
alza / abbassa il volume        metti il volume al 40        muto
pausa      traccia successiva / precedente
apri firefox / chrome / vs code / spotify / …
blocca il PC          minimizza tutto       che tempo fa [a Torino]
```

Tutto il resto — *«scrivi a Marco che arrivo tardi»*, *«apri la cartella dei
download»*, *«riassumi questa pagina»* — prosegue verso il modello. Nel dubbio la
scorciatoia **non** interviene: meglio un secondo in più che un comando frainteso.

Disattivabile con `JARVIS_FAST_INTENTS=off`.

## La voce

Jarvis usa le **voci neurali Microsoft generate dal server** (`edge-tts`): stessa
qualità su qualunque browser, e due voci maschili italiane vere.

| Voce | Carattere |
|---|---|
| **Diego** (predefinita) | maschile, timbro pieno e autorevole — da maggiordomo |
| **Giuseppe** | maschile, più morbido |
| Elsa / Isabella | femminili |

Le scegli dal menu **Voce…** in basso nella HUD: senti subito un'anteprima e la
scelta viene ricordata. Puoi anche fissarla in `.env` con `JARVIS_VOICE`, e
regolare la cadenza con `JARVIS_VOICE_RATE` / `JARVIS_VOICE_PITCH` (valori
negativi = più lento e più grave).

Se il server non riesce a sintetizzare (rete assente), la HUD ripiega
automaticamente sulle voci del browser, che restano in fondo allo stesso menu.

## Scrivere al posto tuo

Jarvis può usare la tastiera:

- **`send_message`** — apre WhatsApp, Telegram, Slack o Discord, cerca il
  contatto e scrive il messaggio nella chat. *Di default non invia*: lascia il
  testo pronto perché tu lo rilegga. L'invio vero richiede una tua conferma
  esplicita a voce.
- **`type_text`** — scrive in qualunque finestra.

> ⚠️ La digitazione va nella finestra **in primo piano**. Per non scrivere per
> sbaglio dentro un documento aperto, `send_message` verifica che l'app sia
> davvero davanti prima di battere un solo tasto, e si ferma se non lo è;
> `type_text` accetta il titolo della finestra attesa e rifiuta di scrivere se
> non corrisponde.

## Limiti del piano gratuito di Gemini

Il piano gratuito concede circa **20 richieste al giorno per modello**. Quando un
modello le esaurisce, Jarvis **passa automaticamente a un altro** modello (ne usa
sette a rotazione, così hai molte più richieste al giorno). Se li esaurisci tutti,
te lo dice chiaramente e ti invita a riprovare più tardi. Per togliere il limite,
attiva la fatturazione su <https://aistudio.google.com/apikey>.

Puoi cambiare la lista dei modelli di riserva con `JARVIS_FALLBACK_MODELS` nel
file `.env` (nomi separati da virgola).

## Note

- Riconoscimento e sintesi vocale usano le API del browser: **serve Chrome o Edge**
  (Firefox non supporta il riconoscimento vocale). Edge dà anche le voci migliori.
- Senza chiave API, Jarvis parte comunque in modalità locale limitata (ora, stato
  sistema, apertura app).
