"""SQLite backups and isolated restore verification; never restores over live data.

Stop the API while backing up the main and bridge databases as one application set.
Keep the destination private: backups contain the same sensitive data as the originals.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('database_integrity_failed')
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {table: db.execute('SELECT count(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0] for table in tables}


def verify(folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('version') != 1 or not isinstance(manifest.get('files'), list) or len(manifest['files']) != 2:
        raise ValueError('invalid_backup_manifest')
    for entry in manifest['files']:
        name = entry['name']
        if Path(name).name != name or '/' in name or '\\' in name or not name.endswith('.sqlite3'):
            raise ValueError('invalid_backup_filename')
    names = {entry['name'] for entry in manifest['files']}
    if len(names) != 2 or not any(name + '.bridge.sqlite3' in names for name in names):
        raise ValueError('invalid_backup_database_set')
    for entry in manifest['files']:
        name = entry['name']
        path = folder / name
        if path.is_symlink() or sha256(path) != entry['sha256']:
            raise ValueError('backup_checksum_mismatch')
        # Restore to a fresh isolated database, then reopen and read it.
        with tempfile.TemporaryDirectory(prefix='sonnic-restore-') as temp:
            restored = Path(temp) / name
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(restored)) as target:
                    source.backup(target)
            if inspect(restored) != entry['rows']:
                raise ValueError('restored_rows_mismatch')
    return {'verified_databases': len(manifest['files']), 'isolated_restore': True}


def backup(main_path, folder):
    main = Path(main_path).resolve()
    sources = [main, Path(str(main) + '.bridge.sqlite3')]
    if any(not path.is_file() or path.is_symlink() for path in sources):
        raise ValueError('database_set_missing')
    folder = Path(folder).resolve()
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifest = {'version': 1, 'files': []}
    for source_path in sources:
        destination = folder / source_path.name
        with closing(sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(destination)) as target:
                source.backup(target)
        os.chmod(destination, 0o600)
        manifest['files'].append({'name':destination.name, 'sha256':sha256(destination), 'rows':inspect(destination)})
    manifest_path = folder / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
    os.chmod(manifest_path, 0o600)
    return verify(folder)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('--main', required=True)
    create.add_argument('--destination', required=True)
    create.add_argument('--api-stopped', action='store_true', required=True)
    check = commands.add_parser('verify')
    check.add_argument('folder')
    args = parser.parse_args()
    print(json.dumps(backup(args.main, args.destination) if args.command == 'create' else verify(args.folder)))
