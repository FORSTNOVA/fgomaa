"""Package the existing Windows app without account data, certificates or run history."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ASSET_DIRS = {'src', 'web', 'config', 'data', 'runtime'}
TOP_DIRS = ASSET_DIRS | {'_internal', 'yaml', '_yaml'}
PRIVATE_NAMES = {'user_account.json', '.app_profile', 'certs', '__pycache__', '.git',
                 'scratch', 'captures', 'logs', '.env'}
PRIVATE_SUFFIXES = {'.key', '.pem', '.p12', '.pfx', '.har', '.pcap', '.pcapng', '.log', '.pyc', '.pyo'}
SECRET = re.compile(rb'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})')


def selected(relative):
    parts = relative.parts
    if any(part.lower() in PRIVATE_NAMES for part in parts) or relative.suffix.lower() in PRIVATE_SUFFIXES:
        return False
    if len(parts) == 1:
        return relative.name in ('FGO-MAA.exe', 'main.py')
    if parts[0] not in TOP_DIRS:
        return False
    # The launcher prioritizes the external copies. Do not package duplicate assets.
    if parts[0] == '_internal' and len(parts) > 1 and parts[1] in ASSET_DIRS:
        return False
    return True


def inventory(app):
    app = app.resolve()
    for name in ('FGO-MAA.exe', 'src', 'web', 'config', 'data', 'runtime/laplace/sources'):
        if not (app / name).exists():
            raise ValueError('Missing release input: ' + name)
    files = []
    for file in sorted(app.rglob('*')):
        if file.is_file() and selected(file.relative_to(app)):
            if file.is_symlink():
                raise ValueError('Release input must not contain symbolic links')
            # Keys/tokens in text or archives abort packaging rather than being published.
            if file.suffix.lower() in ('.json', '.py', '.dart', '.txt', '.md', '.yaml', '.html'):
                if SECRET.search(file.read_bytes()):
                    raise ValueError('Secret material detected in: ' + str(file.relative_to(app)))
            if file.suffix.lower() == '.zip':
                with zipfile.ZipFile(file) as nested:
                    for member in nested.infolist():
                        if member.is_dir():
                            continue
                        path = Path(member.filename)
                        if (any(part.lower() in PRIVATE_NAMES for part in path.parts)
                                or path.suffix.lower() in PRIVATE_SUFFIXES - {'.pyc','.pyo'}):
                            raise ValueError('Private file inside bundled archive: ' + str(file.relative_to(app)))
                        if SECRET.search(nested.read(member)):
                            raise ValueError('Secret material inside bundled archive: ' + str(file.relative_to(app)))
            files.append(file)
    return files


def package(app, output, version):
    app, output = app.resolve(), output.resolve()
    files = inventory(app)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError('Refusing to overwrite release: ' + str(output))
    temporary = output.with_suffix('.zip.tmp')
    manifest = {'version': version, 'account_data_included': False, 'capture_private_keys_included': False,
                'files': []}
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for index, file in enumerate(files):
            relative = file.relative_to(app).as_posix()
            archive.write(file, 'FGO-MAA/' + relative)
            manifest['files'].append({'path': relative, 'bytes': file.stat().st_size,
                                      'sha256': hashlib.sha256(file.read_bytes()).hexdigest()})
            if (index + 1) % 300 == 0:
                print(f'Packaged {index + 1}/{len(files)} files', flush=True)
        for name in ('README.md', 'CHANGELOG.md', 'THIRD_PARTY_NOTICES.md'):
            archive.write(ROOT / name, 'FGO-MAA/' + name)
        archive.writestr('FGO-MAA/RELEASE_MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(temporary) as archive:
        failed = archive.testzip()
        if failed:
            raise ValueError('ZIP integrity check failed: ' + failed)
    temporary.replace(output)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_name(output.name + '.sha256')
    checksum.write_text(digest + '  ' + output.name + '\n', encoding='ascii')
    print(json.dumps({'zip': str(output), 'files': len(files), 'bytes': output.stat().st_size,
                      'sha256': digest}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--app', type=Path, default=ROOT / 'dist/FGO-MAA')
    cli.add_argument('--output', type=Path, required=True)
    cli.add_argument('--version', required=True)
    cli.add_argument('--dry-run', action='store_true')
    args = cli.parse_args()
    if args.dry_run:
        files = inventory(args.app)
        print(json.dumps({'files': len(files), 'bytes': sum(f.stat().st_size for f in files)}))
    else:
        package(args.app, args.output, args.version)
