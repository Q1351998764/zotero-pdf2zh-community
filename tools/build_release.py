"""Build auditable release archives from source; never package live user data."""
import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = 'https://github.com/Q1351998764/zotero-pdf2zh-community'
XPI_NAME = 'zotero-pdf2zh-community.xpi'
SERVER_ROOT_FILES = {
    'server.py', 'engine_runner.py', 'manage_packages.py', 'update_packages.py',
    'start-server.bat', 'requirements.txt', 'index.html', 'favicon.svg', 'bo.mp3', 'notice.json',
}
SERVER_CONFIG_FILES = {'config.json.example', 'config.toml.example', 'venv.json.example'}


def allowed_server_file(path):
    relative = path.relative_to(ROOT / 'server')
    if not path.is_file() or path.is_symlink():
        return False
    if len(relative.parts) == 1:
        return path.name in SERVER_ROOT_FILES
    if len(relative.parts) != 2:
        return False
    directory = relative.parts[0]
    return (
        (directory == 'config' and path.name in SERVER_CONFIG_FILES)
        or (directory == 'utils' and path.suffix == '.py')
        or (directory == 'warmup' and path.suffix in {'.py', '.bat', '.sh'})
        or (directory == 'doc' and path.suffix == '.md')
    )


def tracked_server_files():
    # Git's index is the source boundary. Untracked local files never enter a
    # release, even when they have a source-code extension. Stage reviewed new
    # files before building locally; release CI checks out an immutable commit.
    result = subprocess.run(
        ['git', 'ls-files', '-z', '--cached', '--', 'server'],
        cwd=ROOT, check=True, capture_output=True,
    )
    return sorted({ROOT / name.decode('utf-8') for name in result.stdout.split(b'\0') if name})


def build(destination):
    pkg = json.loads((ROOT / 'plugin/package.json').read_text('utf-8'))
    version = pkg['version']
    server_version = re.search(r'^__version__\s*=\s*["\x27]([^"\x27]+)', (ROOT / 'server/server.py').read_text('utf-8'), re.M)[1]
    if server_version != version:
        raise RuntimeError('Plugin and Server versions differ')
    destination.mkdir(parents=True, exist_ok=True)
    candidates = list((ROOT / 'plugin/build').glob('*.xpi'))
    if len(candidates) != 1:
        raise RuntimeError('Build the plugin before packaging the release')
    source = candidates[0]
    with zipfile.ZipFile(source) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        zotero = manifest['applications']['zotero']
        assert zotero['id'] == pkg['config']['addonID']
        assert manifest['version'] == version
        assert zotero['update_url'].startswith(REPO + '/releases/download/')
        preferences = archive.read('content/preferences.xhtml').decode('utf-8')
        for key in re.findall(r'preference="([^"]+)"', preferences):
            assert key.startswith(pkg['config']['prefsPrefix'] + '.'), key
        assert '__prefsPrefix__' not in archive.read('bootstrap.js').decode('utf-8')
        with zipfile.ZipFile(destination / XPI_NAME, 'w', zipfile.ZIP_DEFLATED) as output:
            for item in archive.infolist():
                if item.filename not in {'LICENSE', 'NOTICE', 'SOURCE.txt'}:
                    output.writestr(item, archive.read(item.filename))
            for name in ('LICENSE', 'NOTICE'):
                output.writestr(name, (ROOT / name).read_bytes())
            output.writestr('SOURCE.txt', f'{REPO}/tree/v{version}\n')
    with zipfile.ZipFile(destination / 'server.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in tracked_server_files():
            if allowed_server_file(path):
                archive.write(path, path.relative_to(ROOT).as_posix())
        for name in ('LICENSE', 'NOTICE', 'README.md', 'CHANGELOG.md'):
            archive.write(ROOT / name, 'server/' + name)
        archive.writestr('server/SOURCE.txt', f'{REPO}/tree/v{version}\n')
    update = {'addons': {pkg['config']['addonID']: {'updates': [{
        'version': version,
        'update_link': f'{REPO}/releases/download/v{version}/{XPI_NAME}',
        'applications': {'zotero': {'strict_min_version': zotero['strict_min_version'], 'strict_max_version': zotero['strict_max_version']}},
    }]}}}
    (destination / 'update.json').write_text(json.dumps(update, indent=2) + '\n', encoding='utf-8')
    files = [destination / name for name in (XPI_NAME, 'server.zip', 'update.json')]
    checksums = ''.join(f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n' for path in files)
    (destination / 'SHA256SUMS.txt').write_text(checksums, encoding='utf-8')
    with zipfile.ZipFile(destination / 'server.zip') as archive:
        names = archive.namelist()
        assert 'server/engine_runner.py' in names and 'server/start-server.bat' in names
        for required in ('utils/engine_compat.py', 'utils/asset_repair.py', 'utils/diagnostics.py', 'warmup/install_managed.py', 'config/venv.json.example'):
            assert 'server/' + required in names, required
        assert not any(name in names for name in ('server/config/config.toml', 'server/config/config.json', 'server/config/venv.json'))
        assert not any('/logs/' in name or '/translated/' in name for name in names)
    print(json.dumps({'version': version, 'artifacts': [str(path) for path in files]}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    build(parser.parse_args().output.resolve())
