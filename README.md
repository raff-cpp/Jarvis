# Jarvis — spazio operativo locale

Assistente personale di Raffaele. Questa cartella contiene la parte **già funzionante
in locale** (senza connettori esterni): stato del sistema, avvio app, to-do, note,
promemoria. Tutto in file di testo/markdown, leggibili e modificabili anche a mano.

## Uso rapido

Dalla cartella `C:\Users\Utente\Jarvis` puoi usare lo shim `jarvis.cmd`:

```
jarvis stato                Stato sistema (CPU, RAM, disco)
jarvis giornata             Apre le app di lavoro (VS Code, Firefox, ...)
jarvis todo                 Mostra la to-do list
jarvis todo "testo"         Aggiunge una voce
jarvis nota "testo"         Salva una nota con data e ora
jarvis nota                 Mostra le note
jarvis promemoria           Mostra i promemoria
jarvis promemoria "testo"   Aggiunge un promemoria
```

Per usarlo da qualsiasi cartella, aggiungi `C:\Users\Utente\Jarvis` al PATH
(oppure chiedi a Jarvis di farlo).

## Struttura

```
Jarvis/
  jarvis.cmd            Entry point da terminale
  README.md            Questo file
  todo.md              To-do list
  promemoria.md        Promemoria
  note.md              Registro note rapide
  note/                Cartella per note lunghe / per-argomento
  scripts/
    jarvis.ps1         Dispatcher dei comandi
    stato-sistema.ps1  Report stato sistema
    avvia-giornata.ps1 Apertura app abituali (personalizzabile)
```

## Cosa funziona ORA (solo locale)

- Stato sistema, apertura app/cartelle/file, gestione file.
- To-do, note, promemoria come liste in markdown.
- Esecuzione comandi terminale, git, script, build/test.

## Cosa serve DOPO (richiede connettori/setup)

- **Meteo e riepilogo mattutino reale** → ricerca web (attivabile) o un connettore meteo.
- **Email / Slack triage** → connettori Gmail/Slack (da autorizzare in claude.ai).
- **Calendario** → connettore Google Calendar.
- **Promemoria che "suonano" a un orario** → serve uno scheduler
  (Attività pianificate di Windows, oppure i cron agent di Claude Code).

Personalizza `scripts/avvia-giornata.ps1` con le tue app preferite.
