param(
    [string]$PublicOrigin = 'http://127.0.0.1:3010',
    [string]$OAuthRedirectUri = '',
    [string]$ClientId = 'sonnic-chatgpt'
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot
$pythonExe = Join-Path $repoRoot '.venv/Scripts/python.exe'
$nodeExe = (Get-Command node -ErrorAction Stop).Source
$nextCli = Join-Path $repoRoot 'node_modules/next/dist/bin/next'
if (!(Test-Path -LiteralPath (Join-Path $repoRoot 'apps/web/.next/BUILD_ID'))) {
    throw 'Run npm.cmd run build first. The lab only uses a production frontend.'
}
foreach ($port in @(3000,8000,3010)) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        throw "Port $port is occupied. Stop the previous Sonnic instance first."
    }
}
$labDir = Join-Path $repoRoot '.cache/bridge-lab'
New-Item -ItemType Directory -Force -Path $labDir | Out-Null
# Environment changes stay within this launcher and its children. Never use the
# user's main diary database or the deferred paid API provider for this lab.
$settingNames = @('AI_MODE','AI_ALLOW_PAID_REQUESTS','SONNIC_DB_PATH','SONNIC_BRIDGE_ORIGIN','SONNIC_BRIDGE_CLIENT_ID','SONNIC_BRIDGE_REDIRECT_URI','API_ORIGIN')
$savedSettings = @{}
foreach ($settingName in $settingNames) { $savedSettings[$settingName] = [Environment]::GetEnvironmentVariable($settingName,'Process') }
$startedProcesses = @()
try {
    $env:AI_MODE = 'disabled'
    $env:AI_ALLOW_PAID_REQUESTS = 'false'
    $env:SONNIC_DB_PATH = Join-Path $labDir 'synthetic.sqlite3'
    $env:SONNIC_BRIDGE_ORIGIN = $PublicOrigin
    $env:SONNIC_BRIDGE_CLIENT_ID = $ClientId
    $env:SONNIC_BRIDGE_REDIRECT_URI = $OAuthRedirectUri
    $env:API_ORIGIN = 'http://127.0.0.1:8000'
    & $pythonExe -c 'import os; from apps.bridge.oauth import BridgeSettings; BridgeSettings(origin=os.environ["SONNIC_BRIDGE_ORIGIN"],client_id=os.environ["SONNIC_BRIDGE_CLIENT_ID"],redirect_uri=os.environ.get("SONNIC_BRIDGE_REDIRECT_URI") or None)'
    if ($LASTEXITCODE -ne 0) { throw 'Invalid bridge origin or OAuth callback.' }
    $startedProcesses += Start-Process -FilePath $pythonExe -ArgumentList @('-m','uvicorn','apps.api.sonnic.main:app','--host','127.0.0.1','--port','8000','--no-access-log') -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $labDir 'api.out.log') -RedirectStandardError (Join-Path $labDir 'api.err.log')
    $startedProcesses += Start-Process -FilePath $nodeExe -ArgumentList @(('"{0}"' -f $nextCli),'start','--hostname','127.0.0.1','--port','3000') -WorkingDirectory (Join-Path $repoRoot 'apps/web') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $labDir 'web.out.log') -RedirectStandardError (Join-Path $labDir 'web.err.log')
    $startedProcesses += Start-Process -FilePath $pythonExe -ArgumentList @('-m','uvicorn','apps.bridge.gateway:app','--host','127.0.0.1','--port','3010','--no-access-log') -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $labDir 'gateway.out.log') -RedirectStandardError (Join-Path $labDir 'gateway.err.log')
    $ready = $false
    for ($i=0; $i -lt 40; $i++) {
        if ($startedProcesses | Where-Object HasExited) { throw 'A lab server stopped. See .cache/bridge-lab/*.err.log.' }
        try {
            $config = Invoke-WebRequest -Uri 'http://127.0.0.1:3010/api/v1/bridge/config' -UseBasicParsing -TimeoutSec 2
            $page = Invoke-WebRequest -Uri 'http://127.0.0.1:3010/bridge' -UseBasicParsing -TimeoutSec 2
            if ($config.StatusCode -eq 200 -and $page.StatusCode -eq 200) { $ready = $true; break }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (!$ready) { throw 'Lab startup timed out.' }
    Write-Host "Synthetic bridge lab: $PublicOrigin/bridge"
    Write-Host 'No tunnel is started by this script. External ChatGPT connection must be configured separately.'
    Write-Host 'For a reviewed HTTPS tunnel, forward ONLY port 3010. Use the same public origin for the job and OAuth.'
    Write-Host 'Press Enter to stop these three lab server processes.'
    Read-Host | Out-Null
} finally {
    foreach ($startedProcess in $startedProcesses) {
        if (!$startedProcess.HasExited) { & taskkill.exe /PID $startedProcess.Id /T /F | Out-Null }
    }
    foreach ($settingName in $settingNames) { [Environment]::SetEnvironmentVariable($settingName,$savedSettings[$settingName],'Process') }
}
