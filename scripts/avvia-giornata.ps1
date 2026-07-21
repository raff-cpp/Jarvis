# Jarvis - Avvia la giornata
# Apre le app di lavoro abituali di Raffaele.
# Uso: powershell -ExecutionPolicy Bypass -File avvia-giornata.ps1
# Personalizza la lista $app aggiungendo/togliendo voci.

$app = @(
    @{ Nome = "VS Code"; Path = "$env:LOCALAPPDATA\Programs\Microsoft VS Code\Code.exe"; Args = @() }
    @{ Nome = "Firefox"; Path = "C:\Program Files\Mozilla Firefox\firefox.exe";          Args = @() }
    # Esempio con progetto: apre worldcup-predictor direttamente in VS Code
    # @{ Nome = "WorldCup in VS Code"; Path = "$env:LOCALAPPDATA\Programs\Microsoft VS Code\Code.exe"; Args = @("C:\Users\Utente\worldcup-predictor") }
)

Write-Output "=== Jarvis - Avvio giornata ==="
foreach ($a in $app) {
    if (Test-Path $a.Path) {
        if ($a.Args.Count -gt 0) { Start-Process -FilePath $a.Path -ArgumentList $a.Args }
        else                     { Start-Process -FilePath $a.Path }
        Write-Output ("Aperto: {0}" -f $a.Nome)
    } else {
        Write-Output ("SALTATO (percorso non trovato): {0} -> {1}" -f $a.Nome, $a.Path)
    }
}
Write-Output "Fatto. Buon lavoro, Raffaele."
