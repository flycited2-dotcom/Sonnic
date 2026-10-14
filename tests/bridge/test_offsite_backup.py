"""Windows recovery checks using synthetic databases, without SSH or live data."""
import json
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import zipfile

import pytest

from scripts.backup_sonnic import backup


pytestmark = pytest.mark.skipif(
    os.name != 'nt' or not shutil.which('pwsh'), reason='Windows DPAPI requires pwsh'
)
SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'backup-offsite.ps1'


def run(*args):
    return subprocess.run(
        ['pwsh', '-NoProfile', '-File', str(SCRIPT), *map(str, args)],
        capture_output=True, text=True, timeout=30,
    )


@pytest.fixture
def encrypted_snapshot(tmp_path):
    main = tmp_path / 'synthetic.sqlite3'
    for path in (main, Path(str(main) + '.bridge.sqlite3')):
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE sample(id PRIMARY KEY, text)')
            db.execute("INSERT INTO sample VALUES(1, 'synthetic dream')")
            db.commit()
    snapshot = tmp_path / 'snapshot'
    backup(main, snapshot)
    zipped = tmp_path / 'snapshot.zip'
    with zipfile.ZipFile(zipped, 'w') as archive:
        for path in snapshot.iterdir():
            archive.write(path, path.name)
    encrypted = tmp_path / 'snapshot.dpapi'
    protector = tmp_path / 'protect.ps1'
    protector.write_text('''param([string]$Source, [string]$Target)
$ErrorActionPreference = 'Stop'
$encrypted = [Security.Cryptography.ProtectedData]::Protect(
    [IO.File]::ReadAllBytes($Source), $null,
    [Security.Cryptography.DataProtectionScope]::CurrentUser)
[IO.File]::WriteAllBytes($Target, $encrypted)
''')
    subprocess.run(['pwsh', '-NoProfile', '-File', str(protector), str(zipped),
                    str(encrypted)], check=True, capture_output=True, timeout=30)
    return encrypted


def test_offsite_archive_restores_without_source_databases(encrypted_snapshot):
    # The encrypted file contains everything the verifier needs.
    before = encrypted_snapshot.read_bytes()
    for path in encrypted_snapshot.parent.rglob('*.sqlite3'):
        path.unlink()
    (encrypted_snapshot.parent / 'snapshot.zip').unlink()
    result = run('-Verify', '-Archive', encrypted_snapshot)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['verified_databases'] == 2
    assert report['isolated_restore'] is True
    assert encrypted_snapshot.read_bytes() == before
    assert 'synthetic dream' not in result.stdout + result.stderr


def test_offsite_corruption_is_rejected(encrypted_snapshot):
    damaged = bytearray(encrypted_snapshot.read_bytes())
    damaged[len(damaged) // 2] ^= 1
    encrypted_snapshot.write_bytes(damaged)
    result = run('-Verify', '-Archive', encrypted_snapshot)
    assert result.returncode != 0
    assert '"isolated_restore":true' not in result.stdout


def test_offsite_never_overwrites_existing_archive(encrypted_snapshot):
    before = encrypted_snapshot.read_bytes()
    result = run('-SshHost', 'unused-host', '-RemoteFolder', '/unused',
                 '-Archive', encrypted_snapshot)
    assert result.returncode != 0
    assert 'refusing to overwrite' in result.stderr
    assert encrypted_snapshot.read_bytes() == before
