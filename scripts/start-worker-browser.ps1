param([switch]$Login)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskProfile = Join-Path $taskRoot 'data/worker/chrome-profile'
$chromePath = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
$projectUrl = 'https://chatgpt.com/'
$listener = Get-NetTCPConnection -LocalPort 9335 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $listenerProcess = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $listener.OwningProcess)
    if ($listenerProcess.CommandLine -notlike ('*' + $taskProfile.Replace('/', '\') + '*')) {
        throw 'Port 9335 belongs to a different profile/process. No process was changed.'
    }
    Write-Output 'Sonnic browser port 9335 is already listening; no process was changed.'
    exit 0
}
New-Item -ItemType Directory -Force -Path $taskProfile | Out-Null
$browserArgs = @('--remote-debugging-address=127.0.0.1', '--remote-debugging-port=9335',
    ('--user-data-dir="' + $taskProfile + '"'), '--no-first-run', '--no-default-browser-check',
    '--hide-crash-restore-bubble', $projectUrl)
if ($Login) {
    # This is an interactive browser for the owner to sign in, not a background helper.
    Start-Process -FilePath $chromePath -ArgumentList $browserArgs
} else {
    Start-Process -FilePath $chromePath -ArgumentList $browserArgs -WindowStyle Hidden
}
Write-Output 'Started the dedicated Sonnic profile on loopback port 9335.'
