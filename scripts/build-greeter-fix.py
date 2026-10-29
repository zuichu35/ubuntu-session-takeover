#!/usr/bin/python3
"""Maintainer-only bundle for the recorded 20261028 deployment, not a generic installer."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
PREFIX = Path('/opt/ubuntu-native-takeover-login-20261028')
DIST = ROOT / 'dist/3392-login-fix-20261028'
NATIVE_BUILD = ROOT / 'build/native-startup-lock-20261028'
NATIVE = NATIVE_BUILD / 'stage' / PREFIX.relative_to('/') / 'build/native-prefix'
GRD = ROOT / 'build/grd-greeter-20261028/stage' / PREFIX.relative_to('/') / 'build/grd-prefix'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true', help='Print local inputs without assembling')
    if parser.parse_args().plan:
        print(json.dumps({'native': str(NATIVE), 'grd': str(GRD), 'destination': str(DIST),
                          'abi': str(ROOT / 'build/native-system-abi.json'),
                          'generic_installer': False}, indent=2))
        return
    if not (NATIVE / 'lib/x86_64-linux-gnu/libmutter-18.so.0').is_file():
        raise SystemExit('Build the isolated startup-lock Mutter first; the older greeter build is incompatible')
    build_record = json.loads((NATIVE_BUILD / 'build-result.json').read_text())
    if build_record.get('privacy_source_sha256') != digest(ROOT / 'takeover/native-privacy.c'):
        raise SystemExit('Startup-lock Mutter was built from different privacy source; rebuild before packaging')
    if not (GRD / 'libexec/gnome-remote-desktop-daemon').is_file():
        raise SystemExit('Build GRD with the real /opt prefix first; a home-prefix binary cannot serve GDM')
    installer = ROOT / 'scripts/install-greeter-fix.py'
    if not installer.is_file():
        raise SystemExit('The reviewed installer is missing')
    # Never overwrite an existing delivered bundle implicitly.
    DIST.mkdir(parents=True, exist_ok=False)
    payload = DIST / 'payload'
    (payload / 'build').mkdir(parents=True)
    shutil.copytree(NATIVE, payload / 'build/native-prefix', symlinks=True)
    shutil.copytree(GRD, payload / 'build/grd-prefix', symlinks=True)
    # Resources/locales come from the new GRD stage; the native stage contains
    # the PAM helper built from this checkout's takeover/local-auth.c.
    helper = payload / 'build/native-prefix/libexec/native-local-auth'
    if not helper.is_file():
        raise RuntimeError('Candidate is missing its locally built PAM helper')
    # GNOME Shell's public GI API must match the installed distribution ABI.
    typelibs = payload / 'build/native-prefix/lib/x86_64-linux-gnu/mutter-18'
    for source in Path('/usr/lib/x86_64-linux-gnu/mutter-18').glob('*.typelib'):
        shutil.copy2(source, typelibs / source.name)
    (payload / 'scripts').mkdir()
    for name in ('native-shell.py', 'native-session.py', 'finish-login-guard.py',
                 'check-native-health.py', 'remote-settings.py'):
        shutil.copy2(ROOT / 'scripts' / name, payload / 'scripts' / name)
    session = payload / 'scripts/native-session.py'
    old = 'vendor/grd-deps/root/usr/lib/x86_64-linux-gnu'
    text = session.read_text()
    if text.count(old) != 1:
        raise RuntimeError('Unexpected source GRD library path')
    session.write_text(text.replace(old, 'build/grd-prefix/lib/x86_64-linux-gnu'))
    (payload / 'takeover').mkdir()
    for name in ('__init__.py', 'native_broker.py', 'monitor_layout.py', 'remote_settings.py',
                 'remote_settings_ui.py', 'login_gateway.py', 'session_health.py',
                 'greeter_identity.py', 'greeter_config.py', 'startup_guard.py'):
        shutil.copy2(ROOT / 'takeover' / name, payload / 'takeover' / name)
    shutil.copy2(ROOT / 'COPYING', payload / 'COPYING')
    recorded = json.loads((ROOT / 'build/native-system-abi.json').read_text())
    abi = {path: digest(Path(path)) for path in recorded}
    if abi != recorded:
        raise RuntimeError('System ABI changed since the source baseline; rebuild and review')
    for destination in (DIST / 'system-abi.json', payload / 'build/native-system-abi.json'):
        destination.write_text(json.dumps(abi, indent=2) + '\n')
    files, symlinks = {}, {}
    for path in sorted(payload.rglob('*')):
        relative = str(path.relative_to(payload))
        if path.is_symlink():
            target = path.readlink()
            if target.is_absolute() or not path.resolve().is_relative_to(payload.resolve()):
                raise RuntimeError('Non-relocatable payload symlink: ' + relative)
            symlinks[relative] = str(target)
        elif path.is_file():
            files[relative] = digest(path)
    (DIST / 'payload-manifest.json').write_text(json.dumps(
        {'version': 1, 'files': files, 'symlinks': symlinks}, indent=2) + '\n')
    shutil.copy2(installer, DIST / 'install.py')
    (DIST / 'build-provenance.json').write_text(json.dumps({
        'native_build': str(NATIVE_BUILD / 'build-result.json'),
        'native_library_sha256': digest(payload / 'build/native-prefix/lib/x86_64-linux-gnu/libmutter-18.so.0'),
        'grd_build': str(ROOT / 'build/grd-greeter-20261028/build-result.json'),
        'grd_sha256': digest(payload / 'build/grd-prefix/libexec/gnome-remote-desktop-daemon'),
        'source_root': str(ROOT), 'target_prefix': str(PREFIX),
        'live_services_changed': False,
    }, indent=2) + '\n')
    print(DIST)


if __name__ == '__main__':
    main()
