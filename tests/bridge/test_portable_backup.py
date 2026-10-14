"""Real age encryption and isolated restore, synthetic SQLite only; no SSH/VPS."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from scripts.backup_sonnic import backup


ROOT = Path(__file__).resolve().parents[2]
AGE = Path(os.environ.get('SONNIC_TEST_AGE', ROOT / '.cache/age-1.3.2/age/age.exe'))
KEYGEN = AGE.with_name('age-keygen.exe')
pytestmark = pytest.mark.skipif(
    os.name != 'nt' or not shutil.which('pwsh') or not AGE.is_file() or not KEYGEN.is_file(),
    reason='Windows, pwsh and real age tools required (SONNIC_TEST_AGE)',
)


def keypair(folder, *, pq=False):
    folder.mkdir()
    identity = folder / 'recovery.txt'
    generated = subprocess.run([str(KEYGEN), *(['-pq'] if pq else []), '-o', str(identity)],
                               capture_output=True, timeout=15)
    assert generated.returncode == 0
    public = subprocess.run([str(KEYGEN), '-y', str(identity)], capture_output=True, timeout=15)
    assert public.returncode == 0
    recipient = folder / 'recipient.txt'
    recipient.write_bytes(public.stdout)
    return identity, recipient


@pytest.fixture
def portable(tmp_path):
    main = tmp_path / 'synthetic.sqlite3'
    for path in (main, Path(str(main) + '.bridge.sqlite3')):
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE sample(id PRIMARY KEY, text)')
            db.execute("INSERT INTO sample VALUES(1, 'synthetic dream for recovery')")
            db.commit()
    snapshot = tmp_path / 'snapshot'
    backup(main, snapshot)
    identity, recipient = keypair(tmp_path / 'keys')
    runner = tmp_path / 'run.ps1'
    runner.write_text('''function global:scp {
    $target = $args[-1]
    Copy-Item -LiteralPath (Join-Path $env:SONNIC_TEST_SNAPSHOT (Split-Path $target -Leaf)) -Destination $target
    $global:LASTEXITCODE = 0
}
& $env:SONNIC_TEST_SCRIPT @args
''', encoding='utf-8')
    return dict(root=tmp_path, snapshot=snapshot, identity=identity, recipient=recipient,
                archive=tmp_path / 'backups/test.age', runner=runner)


def run(case, *, verify=False, identity=None, recipient=None):
    args = ['-Archive', str(case['archive']), '-Encryption', 'Age', '-AgeExecutable', str(AGE),
            '-IdentityFile', str(identity or case['identity'])]
    if verify:
        args += ['-Verify']
    else:
        args += ['-SshHost', 'unused-host', '-RemoteFolder', '/synthetic-snapshot',
                 '-RecipientFile', str(recipient or case['recipient'])]
    result = subprocess.run(['pwsh', '-NoProfile', '-File', str(case['runner']), *args],
                            env=os.environ | {'SONNIC_TEST_SNAPSHOT': str(case['snapshot']),
                                              'SONNIC_TEST_SCRIPT': str(ROOT / 'scripts/backup-offsite.ps1')},
                            capture_output=True, text=True, timeout=40)
    assert 'AGE-SECRET-KEY-' not in result.stdout + result.stderr
    assert 'synthetic dream for recovery' not in result.stdout + result.stderr
    return result


@pytest.mark.parametrize('pq', [False, True])
def test_portable_restore_needs_only_archive_and_independent_key(portable, pq):
    if pq:
        portable['identity'], portable['recipient'] = keypair(portable['root'] / 'pq-keys', pq=True)
    result = run(portable)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['encryption'] == 'age native recovery key'
    before = portable['archive'].read_bytes()
    assert before.startswith(b'age-encryption.org/v1\n')
    recovery = portable['root'] / 'separate-recovery'
    recovery.mkdir()
    key = recovery / 'recovery.txt'
    shutil.copyfile(portable['identity'], key)
    portable['identity'].unlink()
    portable['recipient'].unlink()
    for name in ('manifest.json', 'synthetic.sqlite3', 'synthetic.sqlite3.bridge.sqlite3'):
        (portable['snapshot'] / name).unlink()
    result = run(portable, verify=True, identity=key)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['verified_databases'] == 2 and report['isolated_restore'] is True
    assert portable['archive'].read_bytes() == before


@pytest.mark.parametrize('damage', ['wrong_key', 'ciphertext'])
def test_portable_rejects_wrong_key_and_damaged_ciphertext(portable, damage):
    assert run(portable).returncode == 0
    identity = portable['identity']
    if damage == 'wrong_key':
        identity, _ = keypair(portable['root'] / 'wrong-key')
    else:
        raw = bytearray(portable['archive'].read_bytes())
        raw[-20] ^= 1
        portable['archive'].write_bytes(raw)
    result = run(portable, verify=True, identity=identity)
    assert result.returncode != 0
    assert 'Age operation failed' in result.stderr
    assert 'isolated_restore' not in result.stdout


def test_portable_does_not_publish_unrecoverable_or_overwrite_good_archive(portable):
    wrong_identity, _ = keypair(portable['root'] / 'wrong-key')
    result = run(portable, identity=wrong_identity)
    assert result.returncode != 0
    assert not portable['archive'].exists()
    assert run(portable).returncode == 0
    before = portable['archive'].read_bytes()
    result = run(portable)
    assert result.returncode != 0
    assert 'refusing to overwrite' in result.stderr
    assert portable['archive'].read_bytes() == before


def test_portable_rejects_secret_in_public_recipient_file(portable):
    result = run(portable, recipient=portable['identity'])
    assert result.returncode != 0
    assert 'Expected one native age public recipient' in result.stderr
    assert not portable['archive'].exists()
