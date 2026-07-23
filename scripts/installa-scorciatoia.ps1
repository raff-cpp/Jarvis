# Jarvis - installa la scorciatoia globale Ctrl+Alt+J.
#
# Crea sul Desktop un collegamento "Jarvis" con tasto di scelta rapida
# Ctrl+Alt+J. Da quel momento la combinazione:
#   - avvia Jarvis se e' spento;
#   - lo porta in primo piano (microfono aperto) se e' gia' acceso.
#
# La scorciatoia sta a livello di Windows, quindi funziona anche a Jarvis
# spento -- cosa impossibile per una scorciatoia registrata dentro il processo.
#
# Uso:  powershell -ExecutionPolicy Bypass -File installa-scorciatoia.ps1
# Rilancialo se sposti la cartella del progetto.

$ErrorActionPreference = "Stop"

$radice   = Split-Path -Parent $PSScriptRoot                 # cartella del progetto
$launcher = Join-Path $radice "scripts\jarvis-hotkey.vbs"
$wscript  = Join-Path $env:WINDIR "System32\wscript.exe"
$desktop  = [Environment]::GetFolderPath("Desktop")
$lnkPath  = Join-Path $desktop "Jarvis.lnk"

if (-not (Test-Path $launcher)) {
    Write-Error "Manca il launcher: $launcher"
    exit 1
}

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnkPath)
$sc.TargetPath       = $wscript
$sc.Arguments        = '"' + $launcher + '"'
$sc.WorkingDirectory = $radice
$sc.WindowStyle      = 7                        # 7 = minimizzato (wscript e' comunque invisibile)
$sc.Hotkey           = "Ctrl+Alt+J"            # il tasto di scelta rapida a livello di Windows
$sc.Description       = "Avvia o richiama Jarvis (Ctrl+Alt+J)"
$sc.Save()

Write-Output "Collegamento creato: $lnkPath"
Write-Output "Tasto di scelta rapida: Ctrl+Alt+J -- funziona anche a Jarvis spento."
Write-Output ""
Write-Output "Nota: il collegamento deve restare sul Desktop perche' il tasto"
Write-Output "di scelta rapida sia attivo. Per disinstallarlo, elimina 'Jarvis' dal Desktop."
