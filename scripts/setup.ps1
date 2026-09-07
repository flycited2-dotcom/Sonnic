$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot
Get-Command node -ErrorAction Stop | Out-Null
Get-Command npm.cmd -ErrorAction Stop | Out-Null
$venvPython = Join-Path $repoRoot '.venv/Scripts/python.exe'
if (!(Test-Path -LiteralPath $venvPython)) {
    $bundledPython = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv .venv
    } elseif (Test-Path -LiteralPath $bundledPython) {
        & $bundledPython -m venv .venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv .venv
    } else { throw 'Install Python 3.12 or newer, then run setup again.' }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create Python environment.' }
}
& $venvPython -m pip install -r apps/api/requirements.txt --cache-dir .cache/pip
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
& npm.cmd ci --cache .cache/npm
if ($LASTEXITCODE -ne 0) { throw 'Web dependency installation failed.' }
Write-Host 'Setup complete. Double-click Start-Sonnic.cmd to start Sonnic.'
