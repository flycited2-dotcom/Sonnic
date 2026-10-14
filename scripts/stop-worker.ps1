param([int]$WaitSeconds = 20)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskMarker = Join-Path $taskRoot 'data/worker/stop.requested'
New-Item -ItemType Directory -Force -Path (Split-Path $taskMarker -Parent) | Out-Null
Set-Content -LiteralPath $taskMarker -Value 'Stop after the current job is delivered.'
$taskPython = Join-Path $taskRoot '.venv/Scripts/python.exe'
$stopDeadline = (Get-Date).AddSeconds($WaitSeconds)
do {
    $workerProcesses = @(Get-CimInstance Win32_Process -Filter "name = 'python.exe'" | Where-Object {
        $_.ExecutablePath -eq $taskPython -and $_.CommandLine -match '-m apps\.worker\.main'
    })
    if (-not $workerProcesses) { break }
    Start-Sleep -Seconds 1
} while ((Get-Date) -lt $stopDeadline)
if ($workerProcesses) {
    Write-Output 'Stop requested. Sonnic will finish the active job and then stop; no process was killed.'
} else {
    Write-Output 'Sonnic worker stopped. Its browser profile remains open.'
}
