"""Retention deletes only dated Sonnic archives within the selected local folder."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.name != 'nt' or not shutil.which('pwsh'), reason='Windows retention')
SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/backup-retention.ps1'


def test_retention_keeps_recent_foreign_and_nested_files(tmp_path):
    root = tmp_path / 'Sonnic/Backups'
    root.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    old = 'scheduled-' + (now - timedelta(days=91)).strftime('%Y%m%dT%H%M%SZ')
    recent = 'scheduled-' + (now - timedelta(days=89)).strftime('%Y%m%dT%H%M%SZ')
    for name in (old + '.dpapi', old + '.age', recent + '.age', 'unrelated.age', old + '.txt', 'scheduled-99999999T999999Z.age'):
        (root / name).write_text('test-only')
    nested = root / 'nested'
    nested.mkdir()
    (nested / (old + '.age')).write_text('test-only')
    result = subprocess.run(['pwsh', '-NoProfile', '-File', str(SCRIPT), '-RetentionDays', '90'],
                            env=os.environ | {'LOCALAPPDATA': str(tmp_path)}, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '2'
    assert not (root / (old + '.dpapi')).exists()
    assert not (root / (old + '.age')).exists()
    assert (root / (recent + '.age')).exists()
    assert (root / 'unrelated.age').exists()
    assert (root / (old + '.txt')).exists()
    assert (root / 'scheduled-99999999T999999Z.age').exists()
    assert (nested / (old + '.age')).exists()
