# Jarvis - Dispatcher dei comandi
# Uso: powershell -ExecutionPolicy Bypass -File jarvis.ps1 <comando> [testo]
# Esempi:
#   jarvis.ps1 stato
#   jarvis.ps1 todo "chiamare fornitore"
#   jarvis.ps1 nota "idea: cache dei risultati XGBoost"

param(
    [Parameter(Position = 0)]
    [string]$Comando = "aiuto",
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Resto
)

$root     = Split-Path $PSScriptRoot -Parent
$todoFile = Join-Path $root "todo.md"
$promFile = Join-Path $root "promemoria.md"
$noteFile = Join-Path $root "note.md"
$testo    = ($Resto -join " ").Trim()

function Append-Utf8($path, $line) {
    $enc = New-Object System.Text.UTF8Encoding($false)   # UTF-8 senza BOM
    [System.IO.File]::AppendAllText($path, $line + [Environment]::NewLine, $enc)
}

switch ($Comando.ToLower()) {
    "stato"    { & (Join-Path $PSScriptRoot "stato-sistema.ps1") }
    "giornata" { & (Join-Path $PSScriptRoot "avvia-giornata.ps1") }

    { $_ -in "voce", "avvia", "hud" } {
        $py = Join-Path $root ".venv\Scripts\python.exe"
        if (-not (Test-Path $py)) { $py = "python" }
        Push-Location (Join-Path $root "voice")
        try { & $py "app.py" } finally { Pop-Location }
    }

    "todo" {
        if ($testo) {
            Append-Utf8 $todoFile ("- [ ] {0}" -f $testo)
            Write-Output "Aggiunto ai todo: $testo"
        } else {
            if (Test-Path $todoFile) { Get-Content $todoFile -Encoding UTF8 } else { "Nessun todo." }
        }
    }

    "nota" {
        if ($testo) {
            $stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
            Append-Utf8 $noteFile ("- [{0}] {1}" -f $stamp, $testo)
            Write-Output "Nota salvata."
        } else {
            if (Test-Path $noteFile) { Get-Content $noteFile -Encoding UTF8 } else { "Nessuna nota." }
        }
    }

    "promemoria" {
        if ($testo) {
            Append-Utf8 $promFile ("- [ ] {0}" -f $testo)
            Write-Output "Promemoria aggiunto: $testo"
        } else {
            if (Test-Path $promFile) { Get-Content $promFile -Encoding UTF8 } else { "Nessun promemoria." }
        }
    }

    default {
        Write-Output @"
Jarvis - comandi disponibili:
  voce                 Avvia J.A.R.V.I.S. vocale (interfaccia + cervello AI)
  stato                Stato del sistema (CPU, RAM, disco)
  giornata             Apre le app di lavoro abituali
  todo                 Mostra la to-do list
  todo <testo>         Aggiunge una voce alla to-do list
  nota                 Mostra le note salvate
  nota <testo>         Salva una nota rapida con data e ora
  promemoria           Mostra i promemoria
  promemoria <testo>   Aggiunge un promemoria
"@
    }
}
