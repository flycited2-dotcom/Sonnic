"""Exercise backup orchestration without SSH, live databases or service stops."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.skipif(os.name != 'nt' or not shutil.which('pwsh'), reason='Windows orchestration')
SOURCE = Path(__file__).resolve().parents[2] / 'scripts' / 'backup-cycle.ps1'


@pytest.mark.parametrize('failure,portable,stage,created,verified', [
    ('none', False, 'public_health', True, True),
    ('ssh', False, 'server_snapshot', False, False),
    ('offsite', False, 'offsite_restore', True, False),
    ('http', False, 'public_health', True, True),
    ('none', True, 'public_health', True, True),
    ('portable', True, 'portable_restore', True, True),
    ('http', True, 'public_health', True, True),
    ('canonical_http', True, 'public_health', True, True),
    ('canonical_config', True, 'public_health', True, True),
    ('redirect', True, 'public_health', True, True),
    ('preflight', True, 'preflight', False, False),
])
def test_backup_report_keeps_verified_copy_when_only_http_fails(tmp_path, failure, portable, stage, created, verified):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    shutil.copyfile(SOURCE, scripts / 'backup-cycle.ps1')
    (scripts / 'backup-offsite.ps1').write_text('''param($SshHost, $RemoteFolder, $Archive, $Encryption, $AgeExecutable, $RecipientFile, $IdentityFile)
$stage = if ($Encryption -eq 'Age') { 'portable' } else { 'offsite' }
Add-Content (Join-Path $PSScriptRoot '../calls.txt') $stage
if ($env:SONNIC_TEST_FAILURE -eq $stage) { throw 'private-data-must-not-be-logged' }
[IO.Directory]::CreateDirectory((Split-Path $Archive -Parent)) | Out-Null
[IO.File]::WriteAllText($Archive, 'fake encrypted test artifact')
''', encoding='utf-8')
    runner = tmp_path / 'run.ps1'
    runner.write_text('''$ErrorActionPreference = 'Stop'
function global:ssh {
    Add-Content (Join-Path $PSScriptRoot 'calls.txt') 'ssh'
    $global:LASTEXITCODE = if ($env:SONNIC_TEST_FAILURE -eq 'ssh') { 1 } else { 0 }
}
function global:curl.exe {
    Add-Content (Join-Path $PSScriptRoot 'calls.txt') 'http'
    $endpoint = $args[-1]
    Add-Content (Join-Path $PSScriptRoot 'health-urls.txt') $endpoint
    $failed = $env:SONNIC_TEST_FAILURE -eq 'http' -or
        ($env:SONNIC_TEST_FAILURE -eq 'canonical_http' -and $endpoint -eq 'https://sonrazbor.ru/bridge') -or
        ($env:SONNIC_TEST_FAILURE -eq 'canonical_config' -and $endpoint -eq 'https://sonrazbor.ru/api/v1/bridge/config')
    $global:LASTEXITCODE = if ($failed) { 28 } else { 0 }
    if (-not $failed) {
        if ($env:SONNIC_TEST_FAILURE -eq 'redirect' -and $endpoint -eq 'https://sonrazbor.ru/bridge') { '302' }
        else { '200' }
    }
}
& (Join-Path $PSScriptRoot 'scripts/backup-cycle.ps1') @args
''', encoding='utf-8')
    env = os.environ | {'LOCALAPPDATA': str(tmp_path / 'local'), 'SONNIC_TEST_FAILURE': failure}
    args = []
    if portable:
        preflight_file = tmp_path / 'mock-tool-and-keys'
        if failure != 'preflight':
            preflight_file.write_text('mock-only')
        args = [flag for name in ('PortableRecipientFile', 'PortableIdentityFile', 'AgeExecutable')
                for flag in ('-' + name, str(preflight_file))]
    result = subprocess.run(['pwsh', '-NoProfile', '-File', str(runner), *args], env=env,
                            capture_output=True, text=True, timeout=30)
    assert (result.returncode == 0) == (failure == 'none'), result.stderr
    raw = (tmp_path / 'data/backup-last-run.json').read_text(encoding='utf-8-sig')
    report = json.loads(raw)
    assert report['status'] == ('complete' if failure == 'none' else 'failed')
    assert report['stage'] == stage
    assert report['backup_created'] is created
    assert report['offsite_verified'] is verified
    assert report['public_http_verified'] is (failure == 'none')
    assert report['portable_requested'] is portable
    assert report['portable_verified'] is (portable and stage == 'public_health')
    assert report['legacy_http_verified'] is (stage == 'public_health' and failure != 'http')
    assert report['canonical_http_verified'] is (stage == 'public_health' and failure in {'none', 'canonical_config'})
    assert report['canonical_config_verified'] is (failure == 'none')
    assert report['started_at'] <= report['finished_at']
    assert 'private-data' not in raw
    log = tmp_path / 'calls.txt'
    calls = log.read_text().splitlines() if log.exists() else []
    expected = [] if failure == 'preflight' else ['ssh']
    if failure not in {'preflight', 'ssh'}:
        expected.append('offsite')
        if failure != 'offsite':
            if portable:
                expected.append('portable')
            if failure != 'portable':
                expected.extend(['http'] * (1 if failure == 'http' else
                                            2 if failure in {'canonical_http', 'redirect'} else 3))
    assert calls == expected
    health_log = tmp_path / 'health-urls.txt'
    health_urls = health_log.read_text().splitlines() if health_log.exists() else []
    assert health_urls == [
        'https://sonnic.212.116.115.150.sslip.io/bridge',
        'https://sonrazbor.ru/bridge',
        'https://sonrazbor.ru/api/v1/bridge/config',
    ][:calls.count('http')]
