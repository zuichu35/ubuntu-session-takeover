#!/usr/bin/python3
"""Maintenance-only repair bundle for an existing, matching deployment.

Requires the previous dated dist bundle and its manifest; this is not a clean
installation builder. Assembly never installs or restarts a service. See
docs/DEPLOYMENT.md for the supported publication and deployment boundaries.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
OLD = ROOT / 'dist/3392-login-fix-20261028'
PREFIX = '/opt/ubuntu-native-takeover-login-20261028'
NATIVE = ROOT / 'build/native-startup-lock-20261028/stage' / PREFIX.lstrip('/') / 'build/native-prefix'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(destination):
    if destination.exists():
        raise RuntimeError('Refusing to overwrite an existing bundle: ' + str(destination))
    if not (NATIVE / 'lib/x86_64-linux-gnu/libmutter-18.so.0').exists():
        raise RuntimeError('Build and test the candidate Mutter first')
    destination.mkdir(parents=True)
    payload = destination / 'payload'
    shutil.copytree(OLD / 'payload', payload, symlinks=True)
    # Overlay the rebuilt private compositor. The existing prefix is the true
    # compiled resource path; namespace tests bind this candidate there.
    shutil.rmtree(payload / 'build/native-prefix')  # Only this fresh bundle copy.
    shutil.copytree(NATIVE, payload / 'build/native-prefix', symlinks=True)
    for relative in ('scripts/native-session.py', 'scripts/finish-login-guard.py',
                     'takeover/startup_guard.py', 'takeover/login_gateway.py',
                     'takeover/native_broker.py'):
        shutil.copy2(ROOT / relative, payload / relative)
    supervisor = payload / 'scripts/native-session.py'
    text = supervisor.read_text()
    development = 'vendor/grd-deps/root/usr/lib/x86_64-linux-gnu'
    if text.count(development) != 1:
        raise RuntimeError('Unexpected supervisor dependency path')
    supervisor.write_text(text.replace(development, 'build/grd-prefix/lib/x86_64-linux-gnu'))
    helper = 'build/native-prefix/libexec/native-local-auth'
    if digest(payload / helper) != digest(OLD / 'payload' / helper):
        raise RuntimeError('PAM helper unexpectedly changed')
    for relative in ('system-abi.json',):
        shutil.copy2(OLD / relative, destination / relative)
    shutil.copy2(OLD / 'payload-manifest.json', destination / 'before-handover-manifest.json')
    for name in ('install-handover-fix.py', 'install-greeter-fix.py'):
        shutil.copy2(ROOT / 'scripts' / name, destination / name)
    files, links = {}, {}
    for path in sorted(payload.rglob('*')):
        relative = path.relative_to(payload).as_posix()
        if path.is_symlink():
            if path.readlink().is_absolute() or not path.resolve().is_relative_to(payload):
                raise RuntimeError('Unsafe runtime symlink: ' + relative)
            links[relative] = str(path.readlink())
        elif path.is_file():
            files[relative] = digest(path)
    (destination / 'payload-manifest.json').write_text(json.dumps(
        {'version': 1, 'files': files, 'symlinks': links}, indent=2) + '\n')
    units = {}
    unit_source = ROOT / 'packaging/handover'
    for absolute, name in (
        ('/etc/systemd/user/org.gnome.Shell@.service.d/80-native-login.conf', '80-native-login.conf'),
        ('/etc/systemd/user/ubuntu-native-login-guard.service', 'ubuntu-native-login-guard.service')):
        source = unit_source / name
        if not source.is_file():
            raise RuntimeError('Reviewed unit template is missing: ' + str(source))
        target = destination / 'units' / name
        target.parent.mkdir(exist_ok=True)
        shutil.copy2(source, target)
        units[absolute] = {'source': 'units/' + name, 'sha256': digest(target)}
    (destination / 'units.json').write_text(json.dumps(units, indent=2) + '\n')
    (destination / 'build-provenance.json').write_text(json.dumps({
        'target_prefix': PREFIX, 'source_root': str(ROOT),
        'native_build': str(NATIVE), 'native_sha256': digest(payload / 'build/native-prefix/lib/x86_64-linux-gnu/libmutter-18.so.0'),
        'grd_sha256': digest(payload / 'build/grd-prefix/libexec/gnome-remote-desktop-daemon'),
        'previous_bundle_sha256': digest(destination / 'before-handover-manifest.json'),
        'services_changed': False, 'real_cold_boot_accepted': False}, indent=2) + '\n')
    print(destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    build(args.destination.resolve())
