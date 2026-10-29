#!/usr/bin/env python3
"""Experimental maintainer packaging; not a general GDM installer or a verified fresh build.

Requires patched sources, build dependencies and a diagnostic ABI record.
The historical per-user package layout is distinct from the 20261028 GDM
migration bundles. No runtime compatibility or cold-boot acceptance is implied.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
PREFIX = Path('/opt/ubuntu-native-takeover')
VERSION = '0.2.0~prelogin20261016'
WORK = ROOT / 'build/package'
STAGE = WORK / 'root'
DIST = ROOT / 'dist'
DEPS = [ROOT / 'vendor/native-deps/root/usr', ROOT / 'vendor/grd-deps/root/usr']


def run(args, **kw):
    subprocess.run([str(x) for x in args], check=True, **kw)


def build(component):
    env = dict(os.environ)
    env['PKG_CONFIG_PATH'] = ':'.join(str(d / p) for d in DEPS for p in ('lib/x86_64-linux-gnu/pkgconfig', 'share/pkgconfig'))
    env['LD_LIBRARY_PATH'] = ':'.join(str(d / 'lib/x86_64-linux-gnu') for d in DEPS)
    env['PATH'] = ':'.join(str(d / 'bin') for d in DEPS) + ':' + env['PATH']
    env['PYTHONPATH'] = ':'.join(str(d / 'lib/python3/dist-packages') for d in DEPS)
    env['CPATH'] = '/usr/include/xcb'
    source = ROOT / 'vendor' / ('mutter' if component == 'native' else 'gnome-remote-desktop')
    output = WORK / component
    prefix = PREFIX / f'build/{component}-prefix'
    options = ['-Dintrospection=false', '-Dtests=disabled', '-Dprofiler=false', '-Dinstalled_tests=false',
               '-Ddocs=false', '-Dbash_completion=false', '-Dudev_dir=' + str(prefix / 'lib/udev')] if component == 'native' else [
               '-Dman=false', '-Dtests=false', '-Dsystemd=false',
               '-Dsystemd_sysusers_dir=' + str(prefix / 'lib/sysusers.d'),
               '-Dsystemd_tmpfiles_dir=' + str(prefix / 'lib/tmpfiles.d')]
    with (WORK / f'{component}.log').open('w') as log:
        run(['meson', 'setup', output, source, '--prefix=' + str(prefix), '--buildtype=release',
             '-Dc_link_args=' + ' '.join('-L' + str(d / 'lib/x86_64-linux-gnu') for d in DEPS), *options] +
            (['--reconfigure'] if (output / 'build.ninja').exists() else []), env=env, stdout=log, stderr=log)
        run(['ninja', '-C', output, '-j4'], env=env, stdout=log, stderr=log)
        installed = json.loads(subprocess.check_output(['meson', 'introspect', output, '--installed'], env=env))
        for value in installed.values():
            for path in value if isinstance(value, list) else [value]:
                if not Path(path).is_relative_to(PREFIX):
                    raise RuntimeError('Install escapes package prefix: ' + path)
        run(['meson', 'install', '-C', output, '--no-rebuild', '--destdir', STAGE], env=env, stdout=log, stderr=log)


def copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true', help='Print packaging inputs without building')
    if parser.parse_args().plan:
        print(json.dumps({'prefix': str(PREFIX), 'stage': str(STAGE), 'version': VERSION,
                          'abi': str(ROOT / 'build/native-system-abi.json'),
                          'experimental_maintainer_tool': True,
                          'fresh_full_build_verified': False}, indent=2))
        return
    WORK.mkdir(parents=True, exist_ok=True)
    DIST.mkdir(exist_ok=True)
    if STAGE.exists():
        shutil.rmtree(STAGE)  # Only the dedicated disposable packaging tree.
    STAGE.mkdir()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(build, name) for name in ('native', 'grd')]
        for future in futures:
            future.result()
    app = STAGE / PREFIX.relative_to('/')
    run(['/usr/bin/python3', ROOT / 'scripts/build-local-auth.py'])
    helper = app / 'build/native-prefix/libexec/native-local-auth'
    copy(ROOT / 'build/native-prefix/libexec/native-local-auth', helper)
    helper.chmod(0o755)
    for name in ('native-shell.py', 'native-session.py', 'disable-native.py', 'remote-settings.py', 'patch-kimpanel.py', 'finish-login-guard.py', 'disable-login.py'):
        copy(ROOT / 'scripts' / name, app / 'scripts' / name)
    # Runtime libraries come from declared Ubuntu dependencies, never dev-package symlinks.
    session = app / 'scripts/native-session.py'
    session.write_text(session.read_text().replace('vendor/grd-deps/root/usr/lib/x86_64-linux-gnu',
                                                   'build/grd-prefix/lib/x86_64-linux-gnu'))
    for name in ('__init__.py', 'native_broker.py', 'monitor_layout.py', 'remote_settings.py',
                 'remote_settings_ui.py', 'login_gateway.py', 'session_health.py',
                 'greeter_identity.py', 'greeter_config.py', 'startup_guard.py'):
        copy(ROOT / 'takeover' / name, app / 'takeover' / name)
    copy(ROOT / 'packaging/control.py', app / 'control.py')
    for name in ('COPYING',):
        copy(ROOT / name, app / name)
    copy(ROOT / 'packaging/README.zh-CN.md', app / 'README.md')
    run(['/usr/bin/python3', ROOT / 'scripts/package-compatibility.py', app])
    # Public system-file fingerprints only. No home config or credentials enter the package.
    abi = json.loads((ROOT / 'build/native-system-abi.json').read_text())
    if not abi or any(hashlib.sha256(Path(p).read_bytes()).hexdigest() != h for p, h in abi.items()):
        raise RuntimeError('System changed since native validation; rebuild/retest before packaging')
    (app / 'build/native-system-abi.json').write_text(json.dumps(abi, indent=2) + '\n')
    types = app / 'build/native-prefix/lib/x86_64-linux-gnu/mutter-18'
    for p in Path('/usr/lib/x86_64-linux-gnu/mutter-18').glob('*.typelib'):
        copy(p, types / p.name)
    run(['glib-compile-schemas', app / 'build/native-prefix/share/glib-2.0/schemas'])
    launcher = STAGE / 'usr/bin/ubuntu-native-takeover'
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text('#!/bin/sh\nexec /usr/bin/python3 /opt/ubuntu-native-takeover/control.py "$@"\n')
    launcher.chmod(0o755)
    # The compiled absolute resource paths all target /opt, not the build user's home.
    for tree in (app / 'build/native-prefix', app / 'build/grd-prefix'):
        for p in tree.rglob('*'):
            if p.is_file() and not p.is_symlink() and p.read_bytes()[:4] == b'\x7fELF':
                run(['strip', '--strip-unneeded', p])
    depends = ['gnome-shell', 'libmutter-18-0', 'gnome-remote-desktop',
        'python3', 'python3-gi', 'gir1.2-glib-2.0', 'openssl', 'libfdk-aac2', 'pipewire', 'wireplumber']
    metadata = STAGE / 'DEBIAN'
    metadata.mkdir()
    size = sum(p.stat().st_size for p in app.rglob('*') if p.is_file()) // 1024
    (metadata / 'control').write_text(f'''Package: ubuntu-native-takeover
Version: {VERSION}
Architecture: amd64
Maintainer: Ubuntu Native Takeover contributors <noreply@localhost>
Section: x11
Priority: optional
Depends: {', '.join(depends)}
Installed-Size: {size}
Description: Experimental RDP takeover of the existing native GNOME session
 Keeps native local display/input ownership and adds a physical privacy barrier.
 Explicit per-user setup is required. No running desktop is restarted.
 Physical hardware and Windows acceptance testing is still pending.
''')
    copy(ROOT / 'packaging/prerm', metadata / 'prerm')
    (metadata / 'prerm').chmod(0o755)
    doc = STAGE / 'usr/share/doc/ubuntu-native-takeover'
    copy(ROOT / 'packaging/README.zh-CN.md', doc / 'README.zh-CN.md')
    copy(ROOT / 'COPYING', doc / 'copyright')
    manifest = {str(p.relative_to(app)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(app.rglob('*')) if p.is_file() and not p.is_symlink()}
    (app / 'package-files.json').write_text(json.dumps(manifest, indent=2) + '\n')
    binary = DIST / f'ubuntu-native-takeover_{VERSION}_amd64.deb'
    run(['dpkg-deb', '--root-owner-group', '-Zxz', '--build', STAGE, binary])
    # Full corresponding patched upstream sources, plus our glue and build recipe.
    source_dir = WORK / 'source/ubuntu-native-takeover-source'
    if source_dir.exists():
        shutil.rmtree(source_dir)
    source_dir.mkdir(parents=True)
    source_files = {}
    for project in ('mutter', 'gnome-remote-desktop'):
        repo = ROOT / 'vendor' / project
        if (repo / '.git').exists():
            files = subprocess.check_output(['git', '-C', repo, 'ls-files', '-z']).decode().split('\0')
            if project == 'mutter':
                # Include newly added takeover and distribution-patch sources.
                files += list(json.loads((ROOT / 'build/native-patch-hashes.json').read_text()))
            files = sorted(set(files) - {''})
        else:
            files = json.loads((ROOT / 'SOURCE_FILES.json').read_text())[project]
        source_files[project] = [f for f in files if f]
        for name in files:
            if name:
                p = repo / name
                dest = source_dir / 'vendor' / project / name
                if p.is_symlink():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.symlink_to(p.readlink())
                else:
                    copy(p, dest)
    for directory in ('scripts', 'takeover', 'compat', 'ci', 'patches', 'packaging', 'tests'):
        for p in (ROOT / directory).rglob('*'):
            if p.is_file() and '__pycache__' not in p.parts:
                copy(p, source_dir / p.relative_to(ROOT))
    copy(ROOT / 'COPYING', source_dir / 'COPYING')
    copy(ROOT / 'packaging/README.zh-CN.md', source_dir / 'README.md')
    copy(ROOT / 'build/native-system-abi.json', source_dir / 'build/native-system-abi.json')
    (source_dir / 'SOURCE_FILES.json').write_text(json.dumps(source_files, indent=2) + '\n')
    source_archive = DIST / f'ubuntu-native-takeover_{VERSION}_source.tar.xz'
    with tarfile.open(source_archive, 'w:xz') as archive:
        def anonymous(info):
            info.uid = info.gid = 0
            info.uname = info.gname = 'root'
            return info
        archive.add(source_dir, arcname=source_dir.name, filter=anonymous)
    checksums = DIST / 'SHA256SUMS'
    checksums.write_text(''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n'
                                for p in (binary, source_archive)))
    print(json.dumps({'deb': str(binary), 'source': str(source_archive), 'checksums': str(checksums)}, indent=2))


if __name__ == '__main__':
    main()
