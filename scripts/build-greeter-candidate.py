#!/usr/bin/env python3
"""Build a versioned Mutter candidate into DESTDIR; never install into /opt."""
import hashlib
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'build/native-greeter-20261028'
PREFIX = Path('/opt/ubuntu-native-takeover-login-20261028/build/native-prefix')
STAGE = WORK / 'stage'
DEPS = [ROOT / 'vendor/native-deps/root/usr', ROOT / 'vendor/grd-deps/root/usr']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true', help='Print candidate paths without building')
    if parser.parse_args().plan:
        print(json.dumps({'source': str(ROOT / 'vendor/mutter'), 'prefix': str(PREFIX),
                          'stage': str(STAGE), 'pam_source': str(ROOT / 'takeover/local-auth.c'),
                          'installed_to_live_opt': False}, indent=2))
        return
    if (ROOT / 'takeover/native-privacy.c').read_bytes() != \
            (ROOT / 'vendor/mutter/src/backends/meta-native-privacy.c').read_bytes():
        raise RuntimeError('Native source is stale; run scripts/patch-native.py before building')
    WORK.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env['PKG_CONFIG_PATH'] = ':'.join(str(d / p) for d in DEPS for p in
                                   ('lib/x86_64-linux-gnu/pkgconfig', 'share/pkgconfig'))
    env['LD_LIBRARY_PATH'] = ':'.join(str(d / 'lib/x86_64-linux-gnu') for d in DEPS)
    env['PATH'] = ':'.join(str(d / 'bin') for d in DEPS) + ':' + env['PATH']
    env['PYTHONPATH'] = ':'.join(str(d / 'lib/python3/dist-packages') for d in DEPS)
    env['CPATH'] = '/usr/include/xcb'
    output = WORK / 'mutter'
    with (WORK / 'build.log').open('w') as log:
        def run(args):
            subprocess.run([str(arg) for arg in args], env=env, stdout=log, stderr=log, check=True)
        run(['meson', 'setup', output, ROOT / 'vendor/mutter', '--prefix=' + str(PREFIX),
             '--buildtype=release', '-Dintrospection=false', '-Dtests=disabled', '-Dprofiler=false',
             '-Dinstalled_tests=false', '-Ddocs=false', '-Dbash_completion=false',
             '-Dudev_dir=' + str(PREFIX / 'lib/udev'),
             '-Dc_link_args=' + ' '.join('-L' + str(d / 'lib/x86_64-linux-gnu') for d in DEPS)] +
            (['--reconfigure'] if (output / 'build.ninja').exists() else []))
        run(['ninja', '-C', output, '-j4'])
        installed = json.loads(subprocess.check_output(['meson', 'introspect', output, '--installed'], env=env))
        for destinations in installed.values():
            for destination in destinations if isinstance(destinations, list) else [destinations]:
                if not Path(destination).is_relative_to(PREFIX):
                    raise RuntimeError('Refusing install outside versioned prefix: ' + destination)
        run(['meson', 'install', '-C', output, '--no-rebuild', '--destdir', STAGE])
    candidate = STAGE / PREFIX.relative_to('/')
    types = candidate / 'lib/x86_64-linux-gnu/mutter-18'
    for original in Path('/usr/lib/x86_64-linux-gnu/mutter-18').glob('*.typelib'):
        shutil.copy2(original, types / original.name)
    subprocess.run([sys.executable, str(ROOT / 'scripts/build-local-auth.py')], check=True)
    helper = ROOT / 'build/native-prefix/libexec/native-local-auth'
    (candidate / 'libexec').mkdir(parents=True, exist_ok=True)
    shutil.copy2(helper, candidate / 'libexec/native-local-auth')
    subprocess.run(['glib-compile-schemas', candidate / 'share/glib-2.0/schemas'], check=True)
    manifest = {
        'prefix': str(PREFIX), 'stage': str(STAGE), 'installed_to_live_opt': False,
        'source_revision': subprocess.check_output(['git', '-C', ROOT / 'vendor/mutter',
                                                    'rev-parse', 'HEAD'], text=True).strip(),
        'privacy_source_sha256': hashlib.sha256((ROOT / 'takeover/native-privacy.c').read_bytes()).hexdigest(),
        'files': {str(p.relative_to(candidate)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted(candidate.rglob('*')) if p.is_file() and not p.is_symlink()},
    }
    (WORK / 'build-result.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({key: manifest[key] for key in ('prefix', 'stage', 'installed_to_live_opt')}))


if __name__ == '__main__':
    main()
