$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot
$pythonExe = Join-Path $repoRoot '.venv/Scripts/python.exe'
$nodeExe = (Get-Command node -ErrorAction Stop).Source
$nextCli = Join-Path $repoRoot 'node_modules/next/dist/bin/next'
if (!(Test-Path -LiteralPath $pythonExe) -or !(Test-Path -LiteralPath $nextCli)) {
    throw 'Dependencies are missing. Follow the first-time setup in README.md.'
}
foreach ($port in @(3000, 8000)) {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($listener) { throw "Port $port is already in use. Stop the previous Sonnic window or server first." }
}
$logDir = Join-Path $repoRoot '.cache/runtime'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$apiProcess = $null
$webProcess = $null
try {
    $apiProcess = Start-Process -FilePath $pythonExe -ArgumentList @('-m', 'uvicorn', 'apps.api.sonnic.main:app', '--host', '127.0.0.1', '--port', '8000') -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDir 'api.out.log') -RedirectStandardError (Join-Path $logDir 'api.err.log')
    $webProcess = Start-Process -FilePath $nodeExe -ArgumentList @(('"{0}"' -f $nextCli), 'dev', '--hostname', '127.0.0.1', '--port', '3000') -WorkingDirectory (Join-Path $repoRoot 'apps/web') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDir 'web.out.log') -RedirectStandardError (Join-Path $logDir 'web.err.log')
    Write-Host 'Starting Sonnic. First compilation can take a little time.'
    $ready = $false
    for ($i = 0; $i -lt 45; $i++) {
        if ($apiProcess.HasExited -or $webProcess.HasExited) { throw 'A server stopped. See .cache/runtime/*.err.log.' }
        try {
            $apiHealth = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health/ready' -UseBasicParsing -TimeoutSec 2
            $webHealth = Invoke-WebRequest -Uri 'http://127.0.0.1:3000' -UseBasicParsing -TimeoutSec 3
            if ($apiHealth.StatusCode -eq 200 -and $webHealth.StatusCode -eq 200) { $ready = $true; break }
        } catch { Start-Sleep -Seconds 1 }
    }
    if (!$ready) { throw 'Startup timed out. See .cache/runtime/*.err.log.' }
    Write-Host ''
    Write-Host 'Sonnic is ready: http://127.0.0.1:3000'
    $health = $apiHealth.Content | ConvertFrom-Json
    Write-Host ('AI mode: ' + $health.ai_mode + '. See the page notice before submitting a dream.')
    Write-Host 'Leave this window open. Press Enter here to stop both servers.'
    Read-Host | Out-Null
} finally {
    foreach ($startedProcess in @($webProcess, $apiProcess)) {
        if ($startedProcess -and !$startedProcess.HasExited) {
            # Only process trees started by this invocation; no filesystem mutations.
            & taskkill.exe /PID $startedProcess.Id /T /F | Out-Null
        }
    }
}
