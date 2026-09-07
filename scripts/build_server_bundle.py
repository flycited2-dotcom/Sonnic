"""Build a source-only Linux deployment bundle; never package local secrets/data."""
from pathlib import Path
import hashlib
import tarfile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.cache' / 'deploy' / 'sonnic-source.tar.gz'
EXCLUDE = {'node_modules', '.next', '__pycache__', '.git', '.venv', 'data', '.cache'}


def build():
    files = []
    for folder in ('apps/api', 'apps/bridge', 'apps/web', 'packages/prompt_registry', 'deploy'):
        for path in (ROOT / folder).rglob('*'):
            relative = path.relative_to(ROOT)
            if (path.is_file() and not any(p in EXCLUDE for p in relative.parts)
                    and not path.name.startswith('.env') and path.suffix not in {'.pyc', '.tsbuildinfo'}):
                files.append(path)
    for filename in ('package.json', 'package-lock.json', 'tests/quality/dreams.json'):
        files.append(ROOT / filename)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(OUT, 'w:gz') as archive:
        for path in sorted(files):
            if path.is_symlink():
                raise RuntimeError('Symlinks are not allowed in source bundle')
            archive.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False)
    with tarfile.open(OUT) as archive:
        assert len(archive.getmembers()) == len(files)
        assert all(m.isfile() and '..' not in Path(m.name).parts for m in archive.getmembers())
    sha = hashlib.sha256(OUT.read_bytes()).hexdigest()
    OUT.with_suffix('.gz.sha256').write_text(sha + '  ' + OUT.name + '\n', encoding='ascii')
    print(f'{OUT}\n{len(files)} source files; SHA256 {sha}')


if __name__ == '__main__':
    build()
