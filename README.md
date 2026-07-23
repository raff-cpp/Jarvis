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
| **Scorciatoia globale** | **Ctrl+Alt+J** da qualunque applicazione: **avvia** Jarvis se è spento, altrimenti ne porta la HUD in primo piano col microfono già armato. |
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

Perché **Ctrl+Alt+J** funzioni anche **a Jarvis spento** serve una scorciatoia a
livello di Windows: lanciala una volta con

```
powershell -ExecutionPolicy Bypass -File scripts\installa-scorciatoia.ps1
```

Crea sul Desktop il collegamento *Jarvis* con il tasto di scelta rapida Ctrl+Alt+J:
da spento avvia Jarvis, da acceso lo richiama in primo piano. La combinazione si
cambia dalle proprietà di quel collegamento (e `JARVIS_HOTKEY` nel `.env` resta la
scorciatoia interna usata quando il collegamento non è installato).

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

## Ti riconosce quando arrivi e quando te ne vai

La stessa webcam che guarda le mani si accorge anche se sei alla scrivania. Non
serve attivare nulla in più: basta che **GESTI** sia acceso.

| Cosa succede | Cosa fa Jarvis |
|---|---|
| Ti siedi (volto inquadrato per 1,5 s) | «Bentornato, signore» — e la prima volta della giornata ti fa il riepilogo senza che tu chieda nulla |
| Ti alzi e torni dopo **più di 3 minuti** | ti risaluta |
| Sparisci per 59 minuti | ti avvisa a voce che sta per bloccare |
| Sparisci per **un'ora** | mette in pausa la musica e **blocca il PC** |
| Rientri prima del blocco | annulla il blocco (e ti risaluta) |

**Salutare e bloccare sono due decisioni diverse, con due timer diversi.** È il
punto che conta di più: se condividessero la stessa soglia, portarla a un'ora
significherebbe che ti alzi, torni, e per un'ora non succede più niente. Il
saluto può permettersi di sbagliare — al massimo una frase di troppo — il blocco
no. Da qui i due ordini di grandezza di differenza: **3 minuti** per il saluto,
**un'ora** per il blocco. Sotto i 3 minuti tace: girarti verso l'altro monitor
non deve farti risalutare ogni volta.

**Cosa esce dal browser:** la parola `arrivo` o `uscita`, nient'altro. Nessuna
immagine, nessun volto, nessun dato biometrico lascia mai la pagina — il
riconoscimento gira in locale come quello delle mani, e il server riceve solo
l'esito.

Il blocco automatico è l'azione più invasiva del progetto, quindi ha **quattro
protezioni**: un'ora piena di assenza continuata, un preavviso parlato che ti
lascia un minuto per rientrare, il fatto che non scatta mai se hai dato un
comando a Jarvis di recente, e la difesa dai *salti temporali* qui sotto. La
musica viene fermata **prima** del blocco: dopo, la sessione resta attiva e la
traccia continuerebbe a suonare a schermo spento.

**Un'ora di attesa non è un'ora di osservazione.** Se riduci a icona la HUD, la
copri con un'altra finestra o il PC va in sospensione, il browser mette in pausa
il ciclo video e con esso questo controllo. Senza precauzioni, al risveglio
Jarvis vedrebbe un'assenza apparente di ore e bloccherebbe lo schermo
nell'istante esatto in cui ti risiedi. Quando fra due controlli passa molto più
del dovuto il conteggio **riparte da zero**: di quel buco non si sa nulla, e
l'unica ipotesi onesta è non agire. Serve quindi un'ora *davvero vista* dalla
webcam.

Se il rilevatore di volti non parte (o si rompe mentre gira) la HUD **lo dice**:
*«Rilevamento presenza non disponibile»*. Una presenza che tace perché è rotta
sarebbe indistinguibile da una presenza che tace perché sei lì — e i gesti
continuano a funzionare in ogni caso.

I due tempi si regolano in cima a `voice/web/gestures.js`: `ASSENZA_SALUTO_MS`
e `USCITA_MS`.

Se vuoi solo il saluto, metti `JARVIS_PRESENZA_BLOCCO=off` nel `.env`; per
disattivare tutto, `JARVIS_PRESENZA=off`.

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

**Il reattore pulsa sulla sua voce vera.** Mentre Jarvis parla, l'anello attorno
al nucleo non esegue più un'animazione finta: l'audio della voce passa da un
analizzatore di spettro e le barre si muovono sulle **sillabe reali**, simmetriche
sui due lati. Quando invece sei tu a parlare, l'anello segue il microfono; quando
non c'è audio torna all'animazione di riposo.

## Pilota automatico: usare il computer al posto tuo

Tutto il resto del progetto sa **già** cosa fare («alza il volume» → una
chiamata). Qui no: nessuno sa in anticipo dove sarà il pulsante da premere.

> **«Jarvis, apri le impostazioni e dimmi che versione di Windows ho.»**
>
> *«Apro le impostazioni.»* — *«Scorro in basso.»* — *«Premo Informazioni sul
> dispositivo.»* — *«Windows 11 Pro, versione 25H2.»*

Il ciclo è quello di una persona davanti allo schermo: **guarda → decidi la
prossima mossa → falla → RIGUARDA per vedere com'è andata**. È la rilettura a
ogni passo che gli permette di accorgersi di aver sbagliato invece di
proseguire alla cieca. Non è una macro registrata: se la finestra cambia
posizione, lui la *vede* e si adatta.

Nove mosse possibili: `clicca`, `scrivi`, `premi`, `scorri`, `trascina`,
`apri`, `attendi`, `fatto`, `rinuncia`.

### Le protezioni vengono prima delle funzioni

È la cosa più invasiva del progetto — un programma che clicca da solo — quindi:

| Protezione | Come funziona |
|---|---|
| **Niente azioni irreversibili** | Acquisti, pagamenti, invio di messaggi, cancellazioni e password **non sono tra le mosse disponibili**. Non è questione di buona volontà del modello: non esistono proprio. Se l'obiettivo le richiede, arriva al passo precedente e ti passa la mano. |
| **Racconta prima di agire** | Ogni mossa viene pronunciata *prima* di essere compiuta. |
| **«Basta» ferma le mani** | La stessa parola che lo zittisce a metà frase. La mossa appena annunciata **non viene compiuta**: annuncia, ricontrolla il freno, e solo allora agisce. |
| **Tetto di passi** | Dodici di default. Un pilota che si avvita si ferma da solo invece di bruciare quota. |
| **Non può pilotare se stesso** | I click dentro la finestra di Jarvis vengono rifiutati: sarebbe un anello di retroazione che non si spezza da solo. |

### Cosa devi sapere prima di usarlo

**Costa.** Ogni mossa è una richiesta a Gemini **con una schermata allegata**.
La prova qui sopra sono state 5 richieste in 47 secondi. Sul piano gratuito è
roba da qualche compito al giorno, non da uso continuo.

**Sbaglia.** Cliccherà nel punto sbagliato, ogni tanto. La verifica visiva a
ogni passo serve proprio a permettergli di accorgersene e correggere.

**Non tocca le finestre di sistema elevate.** Windows impedisce a un programma
normale di inviare click a una finestra amministrativa (UAC, alcune parti del
Task Manager). Il pilota se ne accorge perché lo schermo non cambia, e rinuncia.

**Guarda un monitor solo** — quello dove sta la finestra attiva. Con i tuoi due
schermi affiancati l'intero desktop sarebbe 3840×1080, che rimpicciolito per il
modello diventa 1400×394: le scritte dei pulsanti diventano illeggibili e il
pilota clicca a caso. Un monitor solo diventa 1400×788, cioè leggibile.

### Sotto il cofano, se ti serve metterci mano

Il punto fragile è uno solo: il modello indica i punti su una griglia 0–1000
dell'immagine, il mouse vive nei pixel veri del desktop. La traduzione è in
`tools.punto_schermo()` — funzione pura, isolata e coperta da test proprio
perché se sbaglia lei sbagliano **tutti** i click. Cattura e puntatore
dichiarano entrambi la consapevolezza DPI: senza, su uno schermo scalato
vivrebbero in due sistemi di coordinate diversi e ogni click cadrebbe fuori
bersaglio in modo silenzioso e difficilissimo da diagnosticare.

Il tetto di passi si regola con `JARVIS_PILOTA_MAX_PASSI` nel `.env`.

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
