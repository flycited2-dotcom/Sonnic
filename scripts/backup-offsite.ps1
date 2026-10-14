# Copies an existing consistent snapshot; never stops services or reads live DB files.
# DPAPI binds encryption to this Windows account; Age uses a separate recovery key.
[CmdletBinding(DefaultParameterSetName = 'Fetch')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Fetch')]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')]
    [string]$SshHost,
    [Parameter(Mandatory, ParameterSetName = 'Fetch')]
    [ValidatePattern('^/[A-Za-z0-9_/-]+$')]
    [string]$RemoteFolder,
    [Parameter(Mandatory)]
    [string]$Archive,
    [Parameter(Mandatory, ParameterSetName = 'Verify')]
    [switch]$Verify,
    [ValidateSet('DPAPI', 'Age')]
    [string]$Encryption = 'DPAPI',
    [string]$AgeExecutable,
    [string]$RecipientFile,
    [string]$IdentityFile
)
$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'Windows PowerShell 7 is required.' }
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskPython = Join-Path $taskRoot '.venv/Scripts/python.exe'
$taskVerifier = Join-Path $PSScriptRoot 'backup_sonnic.py'
$archivePath = [IO.Path]::GetFullPath($Archive)
if (-not $Verify -and (Test-Path -LiteralPath $archivePath)) {
    throw 'Archive already exists; refusing to overwrite.'
}

if ($Encryption -eq 'Age') {
    if (-not $AgeExecutable -or -not $IdentityFile -or (-not $Verify -and -not $RecipientFile)) {
        throw 'Age requires its executable, a recovery identity and, for creation, a recipient file.'
    }
    $agePath = (Resolve-Path -LiteralPath $AgeExecutable).Path
    $identityPath = (Resolve-Path -LiteralPath $IdentityFile).Path
    # Only native age keys: no plugins, SSH identities or interactive passphrase prompts.
    $identities = @(Get-Content -LiteralPath $identityPath | Where-Object { $_.Trim() -and -not $_.Trim().StartsWith('#') })
    if ($identities.Count -ne 1 -or $identities[0] -cnotmatch '^AGE-SECRET-KEY-(PQ-)?1[023456789ACDEFGHJKLMNPQRSTUVWXYZ]+$') {
        throw 'Expected one native age recovery identity.'
    }
    $identities = $null
    if (-not $Verify) {
        $recipientPath = (Resolve-Path -LiteralPath $RecipientFile).Path
        $recipients = @(Get-Content -LiteralPath $recipientPath | Where-Object { $_.Trim() -and -not $_.Trim().StartsWith('#') })
        if ($recipients.Count -ne 1 -or $recipients[0] -cnotmatch '^age1(pq1)?[023456789acdefghjklmnpqrstuvwxyz]+$') {
            throw 'Expected one native age public recipient.'
        }
    }
}

function Invoke-Age([string[]]$Arguments) {
    $start = [Diagnostics.ProcessStartInfo]::new($agePath)
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.RedirectStandardInput = $true
    foreach ($argument in $Arguments) { $start.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::Start($start)
    try {
        $process.StandardInput.Close()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(60000)) {
            $process.Kill($true)
            $process.WaitForExit()
            throw 'Age operation timed out.'
        }
        # Do not forward diagnostics that can include private paths or key contents.
        if ($process.ExitCode -ne 0) { throw 'Age operation failed; archive was not verified.' }
    } finally { $process.Dispose() }
}

function Set-PrivateDirectory([string]$Path) {
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
    foreach ($sid in @($identity, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Test-Snapshot([string]$Path) {
    $result = & $taskPython $taskVerifier verify $Path 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Snapshot verification failed.' }
    $report = ($result -join "`n") | ConvertFrom-Json
    if ($report.verified_databases -ne 2 -or -not $report.isolated_restore) {
        throw 'Expected two successfully restored databases.'
    }
}

$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$tempPath = Join-Path $tempRoot ('sonnic-offsite-' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($tempPath) | Out-Null
try {
    Set-PrivateDirectory $tempPath
    $snapshot = Join-Path $tempPath 'snapshot'
    $zipPath = Join-Path $tempPath 'snapshot.zip'
    $restored = Join-Path $tempPath 'restored'
    $scope = [Security.Cryptography.DataProtectionScope]::CurrentUser
    if (-not $Verify) {
        # Only fixed filenames are fetched over authenticated SSH; no remote shell command.
        [IO.Directory]::CreateDirectory($snapshot) | Out-Null
        foreach ($name in @('manifest.json', 'synthetic.sqlite3', 'synthetic.sqlite3.bridge.sqlite3')) {
            & scp -q -o BatchMode=yes -o ConnectTimeout=15 "${SshHost}:${RemoteFolder}/$name" (Join-Path $snapshot $name)
            if ($LASTEXITCODE -ne 0) { throw 'Snapshot download failed.' }
        }
        Test-Snapshot $snapshot
        [IO.Compression.ZipFile]::CreateFromDirectory($snapshot, $zipPath)
        if ($Encryption -eq 'Age') {
            $ageTemp = Join-Path $tempPath 'snapshot.age'
            Invoke-Age @('--encrypt', '--recipients-file', $recipientPath, '--output', $ageTemp, $zipPath)
            $encrypted = [IO.File]::ReadAllBytes($ageTemp)
        } else {
            $encrypted = [Security.Cryptography.ProtectedData]::Protect(
                [IO.File]::ReadAllBytes($zipPath), $null, $scope)
        }
    } else {
        $encrypted = [IO.File]::ReadAllBytes($archivePath)
    }
    # Verify the encrypted artifact itself, rather than just its unencrypted source.
    if ($Encryption -eq 'Age') {
        $ageTemp = Join-Path $tempPath 'snapshot.age'
        [IO.File]::WriteAllBytes($ageTemp, $encrypted)
        Invoke-Age @('--decrypt', '--identity', $identityPath, '--output', $zipPath, $ageTemp)
    } else {
        $plain = [Security.Cryptography.ProtectedData]::Unprotect($encrypted, $null, $scope)
        [IO.File]::WriteAllBytes($zipPath, $plain)
        [Array]::Clear($plain, 0, $plain.Length)
    }
    $zip = [IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $names = @($zip.Entries | ForEach-Object { $_.FullName })
        if ($names.Count -ne 3 -or @($names | Select-Object -Unique).Count -ne 3 -or
            $names -notcontains 'manifest.json' -or
            @($names | Where-Object { $_ -ne 'manifest.json' -and $_ -notmatch '^[A-Za-z0-9_-][A-Za-z0-9_.-]*\.sqlite3$' }).Count) {
            throw 'Unexpected snapshot archive entries.'
        }
    } finally { $zip.Dispose() }
    [IO.Compression.ZipFile]::ExtractToDirectory($zipPath, $restored)
    Test-Snapshot $restored
    if (-not $Verify) {
        $parent = Split-Path $archivePath -Parent
        if (-not (Test-Path -LiteralPath $parent)) {
            [IO.Directory]::CreateDirectory($parent) | Out-Null
            Set-PrivateDirectory $parent
        }
        $stream = [IO.File]::Open($archivePath, [IO.FileMode]::CreateNew)
        try { $stream.Write($encrypted, 0, $encrypted.Length) } finally { $stream.Dispose() }
    }
    $encryptionLabel = if ($Encryption -eq 'Age') { 'age native recovery key' } else { 'Windows DPAPI CurrentUser' }
    @{ verified_databases = 2; isolated_restore = $true; encryption = $encryptionLabel;
       archive = $archivePath; sha256 = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash } |
        ConvertTo-Json -Compress
} finally {
    # The only recursive delete target is the unique, checked temporary child created above.
    $resolvedTemp = [IO.Path]::GetFullPath($tempPath)
    if ((Split-Path $resolvedTemp -Parent).TrimEnd('\') -ne $tempRoot.TrimEnd('\') -or
        (Split-Path $resolvedTemp -Leaf) -notmatch '^sonnic-offsite-[a-f0-9]{32}$') {
        throw 'Unexpected cleanup path; refusing deletion.'
    }
    Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
}
