# Explicit backup entry point for the daily Codex automation. No ChatGPT/model calls.
param(
    [string]$PortableRecipientFile,
    [string]$PortableIdentityFile,
    [string]$AgeExecutable,
    [ValidateRange(0, 3650)][int]$RetentionDays = 0
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$snapshotName = 'scheduled-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
$archivePath = Join-Path $env:LOCALAPPDATA "Sonnic/Backups/$snapshotName.dpapi"
$mutex = [Threading.Mutex]::new($false, 'Local\SonnicBackupCycle')
if (-not $mutex.WaitOne(0)) { throw 'Another Sonnic backup is running.' }
$report = [ordered]@{
    started_at = [DateTime]::UtcNow.ToString('o'); snapshot = $snapshotName
    status = 'running'; stage = 'preflight'; backup_created = $false
    offsite_verified = $false; public_http_verified = $false
    legacy_http_verified = $false; canonical_http_verified = $false
    canonical_config_verified = $false
    portable_requested = [bool]($PortableRecipientFile -or $PortableIdentityFile -or $AgeExecutable)
    portable_verified = $false
}
try {
    # Owner-selected settings live outside Git and contain paths, never key contents.
    $settingsPath = Join-Path $env:LOCALAPPDATA 'Sonnic/backup-settings.json'
    if (-not ($PortableRecipientFile -or $PortableIdentityFile -or $AgeExecutable) -and
        (Test-Path -LiteralPath $settingsPath -PathType Leaf)) {
        $savedSettings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
        $PortableRecipientFile = $savedSettings.portable_recipient_file
        $PortableIdentityFile = $savedSettings.portable_identity_file
        $AgeExecutable = $savedSettings.age_executable
        if (-not $PSBoundParameters.ContainsKey('RetentionDays')) {
            $RetentionDays = [int]$savedSettings.retention_days
            if ($RetentionDays -lt 0 -or $RetentionDays -gt 3650) { throw 'Invalid backup retention.' }
        }
    }
    $report.portable_requested = [bool]($PortableRecipientFile -or $PortableIdentityFile -or $AgeExecutable)
    $report.retention_days = $RetentionDays
    if ($report.portable_requested) {
        foreach ($path in @($PortableRecipientFile, $PortableIdentityFile, $AgeExecutable)) {
            if (-not $path -or -not (Test-Path -LiteralPath $path -PathType Leaf)) {
                throw 'Portable backup requires existing recipient, identity and age executable files.'
            }
        }
    }
    $report.stage = 'server_snapshot'
    & ssh -o BatchMode=yes -o ConnectTimeout=15 sprintbox-itp "sh /opt/sonnic/backup-server.sh $snapshotName"
    if ($LASTEXITCODE -ne 0) { throw 'Server backup failed or was deferred; no offsite copy created.' }
    $report.backup_created = $true
    $report.stage = 'offsite_restore'
    & (Join-Path $PSScriptRoot 'backup-offsite.ps1') -SshHost sprintbox-itp `
        -RemoteFolder "/opt/sonnic/backups/$snapshotName" -Archive $archivePath
    if (-not (Test-Path -LiteralPath $archivePath)) { throw 'Offsite archive not created.' }
    $report.offsite_verified = $true
    if ($report.portable_requested) {
        $report.stage = 'portable_restore'
        $portableArchive = [IO.Path]::ChangeExtension($archivePath, '.age')
        & (Join-Path $PSScriptRoot 'backup-offsite.ps1') -SshHost sprintbox-itp `
            -RemoteFolder "/opt/sonnic/backups/$snapshotName" -Archive $portableArchive `
            -Encryption Age -AgeExecutable $AgeExecutable -RecipientFile $PortableRecipientFile `
            -IdentityFile $PortableIdentityFile
        if (-not (Test-Path -LiteralPath $portableArchive)) { throw 'Portable archive not created.' }
        $report.portable_verified = $true
    }
    $report.stage = 'public_health'
    # Check compatibility and the published domain without creating a session or
    # exposing the config body. Curl verifies TLS; redirects and non-200 replies
    # do not count as successful health checks. Each endpoint is requested once.
    foreach ($healthCheck in @(
        @{ url = 'https://sonnic.212.116.115.150.sslip.io/bridge'; flag = 'legacy_http_verified' },
        @{ url = 'https://sonrazbor.ru/bridge'; flag = 'canonical_http_verified' },
        @{ url = 'https://sonrazbor.ru/api/v1/bridge/config'; flag = 'canonical_config_verified' }
    )) {
        $httpStatus = & curl.exe --fail --silent --show-error --max-time 20 `
            --proto '=https' --request GET --output NUL --write-out '%{http_code}' $healthCheck.url
        if ($LASTEXITCODE -ne 0 -or $httpStatus -ne '200') {
            throw 'Backup created, but public site health check failed.'
        }
        $report[$healthCheck.flag] = $true
    }
    $report.public_http_verified = $true
    if ($RetentionDays -gt 0) {
        $report.retention_pruned = & (Join-Path $PSScriptRoot 'backup-retention.ps1') -RetentionDays $RetentionDays
    }
    $report.status = 'complete'
    Write-Output 'Backup cycle complete: encrypted offsite restore verified; site healthy.'
} catch {
    $report.status = 'failed'
    throw
} finally {
    try {
        # Fixed metadata only. Never serialize exception messages, DB contents or keys.
        $report.finished_at = [DateTime]::UtcNow.ToString('o')
        $reportFolder = Join-Path $taskRoot 'data'
        [IO.Directory]::CreateDirectory($reportFolder) | Out-Null
        $reportPath = Join-Path $reportFolder 'backup-last-run.json'
        $reportTemp = Join-Path $reportFolder 'backup-last-run.tmp'
        $report | ConvertTo-Json | Set-Content -LiteralPath $reportTemp -Encoding utf8
        Move-Item -LiteralPath $reportTemp -Destination $reportPath -Force
    } finally {
        $mutex.ReleaseMutex()
        $mutex.Dispose()
    }
}
