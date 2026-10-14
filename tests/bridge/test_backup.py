import json
from pathlib import Path
import sqlite3

import pytest

from scripts.backup_sonnic import backup, verify


def database_set(tmp_path):
    main = tmp_path / 'site.sqlite3'
    for path in (main, Path(str(main)+'.bridge.sqlite3')):
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE sample(id PRIMARY KEY, text)')
            db.execute('INSERT INTO sample VALUES(1,?)', ('synthetic private text',))
    return main


def test_backup_restores_original_after_live_data_changes(tmp_path):
    main = database_set(tmp_path)
    folder = tmp_path/'backup'
    assert backup(main, folder) == {'verified_databases':2,'isolated_restore':True}
    with sqlite3.connect(main) as db:
        db.execute('DELETE FROM sample')
    assert verify(folder)['verified_databases'] == 2
    with sqlite3.connect(folder/main.name) as db:
        assert db.execute('SELECT text FROM sample').fetchone()[0] == 'synthetic private text'
    assert 'synthetic private text' not in (folder/'manifest.json').read_text()


def test_verify_does_not_touch_live_database_and_cleans_isolated_restore(tmp_path, monkeypatch):
    main = database_set(tmp_path)
    folder = tmp_path/'backup'
    backup(main, folder)
    with sqlite3.connect(main) as db:
        db.execute('INSERT INTO sample VALUES(2,?)', ('new live row',))
    live_before = main.read_bytes()
    backup_before = {
        path.name: path.read_bytes()
        for path in folder.iterdir()
    }
    restore_root = tmp_path/'isolated-restores'
    restore_root.mkdir()
    monkeypatch.setattr('scripts.backup_sonnic.tempfile.tempdir', str(restore_root))

    assert verify(folder) == {'verified_databases':2,'isolated_restore':True}

    assert main.read_bytes() == live_before
    assert {
        path.name: path.read_bytes()
        for path in folder.iterdir()
    } == backup_before
    assert list(restore_root.iterdir()) == []


def test_backup_never_overwrites_and_requires_both_databases(tmp_path):
    main = database_set(tmp_path)
    folder = tmp_path/'backup'
    backup(main, folder)
    with pytest.raises(FileExistsError):
        backup(main, folder)
    with pytest.raises(ValueError, match='database_set_missing'):
        backup(tmp_path/'absent.sqlite3', tmp_path/'new')
    assert not (tmp_path/'new').exists()


def test_modified_backup_is_rejected(tmp_path):
    main = database_set(tmp_path)
    folder = tmp_path/'backup'
    backup(main, folder)
    with (folder/main.name).open('ab') as stream:
        stream.write(b'changed')
    with pytest.raises(ValueError, match='backup_checksum_mismatch'):
        verify(folder)


@pytest.mark.parametrize('name',['../site.sqlite3','..\\site.sqlite3','/etc/site.sqlite3'])
def test_manifest_cannot_read_outside_backup(tmp_path,name):
    main = database_set(tmp_path)
    folder=tmp_path/'backup'
    backup(main,folder)
    manifest=json.loads((folder/'manifest.json').read_text())
    manifest['files'][0]['name']=name
    (folder/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='invalid_backup_filename'):
        verify(folder)


@pytest.mark.parametrize('defect', ['missing', 'duplicate', 'unrelated'])
def test_manifest_requires_main_and_corresponding_bridge_database(tmp_path, defect):
    folder = tmp_path / 'backup'
    backup(database_set(tmp_path), folder)
    manifest = json.loads((folder / 'manifest.json').read_text())
    if defect == 'missing':
        manifest['files'].pop()
    elif defect == 'duplicate':
        manifest['files'][1] = manifest['files'][0].copy()
    else:
        manifest['files'][1]['name'] = 'unrelated.sqlite3'
    (folder / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='invalid_backup_'):
        verify(folder)
