# Called only after a newly created backup passed restore and public health checks.
param([Parameter(Mandatory)][ValidateRange(1, 3650)][int]$RetentionDays)
$ErrorActionPreference = 'Stop'
$taskArchiveRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Sonnic/Backups'))
if (-not (Test-Path -LiteralPath $taskArchiveRoot -PathType Container)) { return 0 }
$taskFolder = Get-Item -LiteralPath $taskArchiveRoot
if ($taskFolder.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Backup archive directory cannot be a reparse point.' }
$taskCutoff = [DateTime]::UtcNow.AddDays(-$RetentionDays)
$taskRemoved = 0
foreach ($taskArchive in Get-ChildItem -LiteralPath $taskArchiveRoot -File) {
    if ($taskArchive.Name -cnotmatch '^scheduled-([0-9]{8}T[0-9]{6}Z)\.(dpapi|age)$' -or
        ($taskArchive.Attributes -band [IO.FileAttributes]::ReparsePoint)) { continue }
    try {
        $taskCreated = [DateTime]::ParseExact($Matches[1], 'yyyyMMddTHHmmssZ',
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal)
    } catch { continue }
    $taskTarget = [IO.Path]::GetFullPath($taskArchive.FullName)
    if ([IO.Path]::GetDirectoryName($taskTarget) -ne $taskArchiveRoot) { throw 'Backup retention target is outside the archive directory.' }
    if ($taskCreated -lt $taskCutoff) {
        Remove-Item -LiteralPath $taskTarget
        $taskRemoved++
    }
}
return $taskRemoved
