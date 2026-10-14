param([switch]$Resume, [switch]$SshTunnel)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskPython = Join-Path $taskRoot '.venv/Scripts/python.exe'
$taskLogs = Join-Path $taskRoot 'data/worker'
New-Item -ItemType Directory -Force -Path $taskLogs | Out-Null
if ($Resume) {
    Remove-Item -LiteralPath (Join-Path $taskLogs 'stop.requested') -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $taskLogs 'pause.requested') -ErrorAction SilentlyContinue
} elseif ((Test-Path -LiteralPath (Join-Path $taskLogs 'stop.requested')) -or
          (Test-Path -LiteralPath (Join-Path $taskLogs 'pause.requested'))) {
    Write-Output 'Sonnic is stopped or paused. Explicit -Resume is required; no browser was opened.'
    exit 0
}
$existing = Get-CimInstance Win32_Process -Filter "name = 'python.exe'" | Where-Object {
    $_.ExecutablePath -eq $taskPython -and $_.CommandLine -match '-m apps\.worker\.main'
}
if ($existing) {
    Write-Output 'Sonnic worker is already running.'
    exit 0
}
& (Join-Path $PSScriptRoot 'start-worker-browser.ps1')
$watchArgs = @('-NoProfile', '-File', ('"' + (Join-Path $PSScriptRoot 'watch-worker.ps1') + '"'))
if ($SshTunnel) { $watchArgs += '-SshTunnel' }
Start-Process -FilePath (Get-Command pwsh).Source -ArgumentList $watchArgs `
    -WorkingDirectory $taskRoot -WindowStyle Hidden
Write-Output 'Started the Sonnic worker. Logs are in data/worker; no secrets are printed.'
