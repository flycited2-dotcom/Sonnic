param([switch]$SshTunnel)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskPython = Join-Path $taskRoot '.venv/Scripts/python.exe'
$taskLogs = Join-Path $taskRoot 'data/worker'
$watchMutex = [System.Threading.Mutex]::new($false, 'Local\SonnicWorkerWatchdog')
if (-not $watchMutex.WaitOne(0)) { exit 0 }
$crashCount = 0
$tunnelProcess = $null
$originalServer = $env:SONNIC_WORKER_SERVER
function Start-SonnicTunnel {
    # Never reuse an unknown listener or expose the service outside loopback.
    if (Get-NetTCPConnection -LocalPort 3181 -State Listen -ErrorAction SilentlyContinue) {
        throw 'Sonnic tunnel port 3181 is occupied; no process was changed.'
    }
    $tunnelArgs = @('-N', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=10',
        '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
        '-L', '127.0.0.1:3181:127.0.0.1:3180', 'sprintbox-itp')
    $process = Start-Process -FilePath (Get-Command ssh).Source -ArgumentList $tunnelArgs `
        -WindowStyle Hidden -PassThru -RedirectStandardError (Join-Path $taskLogs 'tunnel-error.log')
    return $process
}
try {
    if ($SshTunnel) {
        $tunnelProcess = Start-SonnicTunnel
        # This override is inherited only by this worker. The public URL and
        # browser profile are unchanged; SSH encrypts the PC-to-VPS transport.
        $env:SONNIC_WORKER_SERVER = 'http://127.0.0.1:3181'
    }
    while (-not (Test-Path -LiteralPath (Join-Path $taskLogs 'stop.requested')) -and
           -not (Test-Path -LiteralPath (Join-Path $taskLogs 'pause.requested'))) {
        & (Join-Path $PSScriptRoot 'start-worker-browser.ps1')
        $workerProcess = Start-Process -FilePath $taskPython -ArgumentList @('-m', 'apps.worker.main') `
            -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $taskLogs 'worker.log') `
            -RedirectStandardError (Join-Path $taskLogs 'worker-error.log')
        $retryTunnelAfter = [DateTime]::UtcNow
        while (-not $workerProcess.WaitForExit(5000)) {
            if ($SshTunnel -and ($null -eq $tunnelProcess -or $tunnelProcess.HasExited) -and
                [DateTime]::UtcNow -ge $retryTunnelAfter) {
                try {
                    $tunnelProcess = Start-SonnicTunnel
                    Add-Content -LiteralPath (Join-Path $taskLogs 'watchdog.log') -Value ((Get-Date -Format o) + ' reconnecting Sonnic SSH transport')
                } catch {
                    # No raw exception or SSH output in the summary log.
                    Add-Content -LiteralPath (Join-Path $taskLogs 'watchdog.log') -Value ((Get-Date -Format o) + ' Sonnic SSH transport unavailable; retry in 30 seconds')
                }
                $retryTunnelAfter = [DateTime]::UtcNow.AddSeconds(30)
            }
        }
        if ((Test-Path -LiteralPath (Join-Path $taskLogs 'stop.requested')) -or
            (Test-Path -LiteralPath (Join-Path $taskLogs 'pause.requested'))) { break }
        $crashCount++
        if ($crashCount -ge 3) {
            Set-Content -LiteralPath (Join-Path $taskLogs 'pause.requested') -Value 'worker_crash_limit'
            Add-Content -LiteralPath (Join-Path $taskLogs 'watchdog.log') -Value ((Get-Date -Format o) + ' stopped after three unexpected worker exits; explicit resume required')
            break
        }
        Add-Content -LiteralPath (Join-Path $taskLogs 'watchdog.log') -Value ((Get-Date -Format o) + ' worker exited; restarting in 15 seconds')
        Start-Sleep -Seconds 15
    }
} finally {
    if ($null -ne $tunnelProcess -and -not $tunnelProcess.HasExited) {
        $tunnelProcess.Kill()
        $tunnelProcess.WaitForExit(5000) | Out-Null
    }
    $env:SONNIC_WORKER_SERVER = $originalServer
    $watchMutex.ReleaseMutex()
    $watchMutex.Dispose()
}
