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
| **Push-to-talk** | Clicca il microfono e parla. |
| **Sempre in ascolto** | Attiva *Ascolto continuo*: risponde solo dopo la parola «Jarvis». |
| **Senza parola chiave** | Ascolto continuo + togli *Parola d'attivazione*: esegue qualunque frase. |
| **Testo** | Scrivi nel campo in basso e premi Invio. |

Puoi dire tutto in una frase (*«Jarvis, apri Firefox»*) oppure solo *«Jarvis»*, aspettare
che il reattore si illumini e poi dare il comando.

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

Sulle chiavi del piano gratuito la ricerca Google integrata ha **quota separata** e
risponde `429`, anche quando il function calling funziona benissimo. In quel caso:

- con `JARVIS_WEB_SEARCH=auto` Jarvis se ne accorge al primo turno, la disattiva per
  la sessione e va avanti;
- con `off` evita anche quella chiamata sprecata;
- in entrambi i casi ripiega su `Invoke-RestMethod` via PowerShell per i dati in
  tempo reale (es. `wttr.in` per il meteo), e se non può dirti un dato te lo dice
  invece di inventarlo.

## Note

- Riconoscimento e sintesi vocale usano le API del browser: **serve Chrome o Edge**
  (Firefox non supporta il riconoscimento vocale).
- Senza chiave API, Jarvis parte comunque in modalità locale limitata (ora, stato
  sistema, apertura app).
