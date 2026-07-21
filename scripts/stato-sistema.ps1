# Jarvis - Stato del sistema
# Uso: powershell -ExecutionPolicy Bypass -File stato-sistema.ps1

$os  = Get-CimInstance Win32_OperatingSystem
$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
$ramTotal = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
$ramFree  = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
$ramUsed  = [math]::Round($ramTotal - $ramFree, 1)
$ramPct   = [math]::Round(($ramUsed / $ramTotal) * 100, 0)
$uptime   = (Get-Date) - $os.LastBootUpTime

Write-Output "=== Jarvis - Stato sistema ($(Get-Date -Format 'dd/MM/yyyy HH:mm')) ==="
Write-Output ("Sistema : {0}" -f $os.Caption)
Write-Output ("Uptime  : {0}g {1}h {2}m" -f $uptime.Days, $uptime.Hours, $uptime.Minutes)
Write-Output ("CPU     : {0}% di carico" -f $cpu)
Write-Output ("RAM     : {0} / {1} GB usati ({2}%)" -f $ramUsed, $ramTotal, $ramPct)

Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object {
    $freeGB  = [math]::Round($_.FreeSpace / 1GB, 1)
    $totGB   = [math]::Round($_.Size / 1GB, 1)
    $usedPct = [math]::Round((($_.Size - $_.FreeSpace) / $_.Size) * 100, 0)
    Write-Output ("Disco {0} : {1} GB liberi su {2} GB ({3}% usato)" -f $_.DeviceID, $freeGB, $totGB, $usedPct)
}

$bat = Get-CimInstance Win32_Battery
if ($bat) { Write-Output ("Batteria: {0}%" -f $bat.EstimatedChargeRemaining) }
else      { Write-Output "Batteria: n/d (desktop o assente)" }
