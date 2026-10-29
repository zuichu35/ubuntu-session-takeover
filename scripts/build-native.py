#!/usr/bin/env python3
"""Build the native compositor privately; never replace the running shell."""
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
subprocess.run([sys.executable, str(root / 'scripts/build-local-auth.py')], check=True)
deps = [root / 'vendor/native-deps/root/usr', root / 'vendor/grd-deps/root/usr']
env = dict(os.environ)
env['PKG_CONFIG_PATH'] = ':'.join(str(d / p) for d in deps for p in
                               ('lib/x86_64-linux-gnu/pkgconfig', 'share/pkgconfig'))
env['LD_LIBRARY_PATH'] = ':'.join(str(d / 'lib/x86_64-linux-gnu') for d in deps)
env['PATH'] = ':'.join(str(d / 'bin') for d in deps) + ':' + env['PATH']
env['PYTHONPATH'] = ':'.join(str(d / 'lib/python3/dist-packages') for d in deps)
# Locally extracted xcb extension headers include their installed core sibling.
env['CPATH'] = '/usr/include/xcb' + (':' + env['CPATH'] if env.get('CPATH') else '')
build = root / 'build/native-mutter'
if not (build / 'build.ninja').exists() or '--reconfigure' in sys.argv:
    subprocess.run(['meson', 'setup', str(build), str(root / 'vendor/mutter'),
                    '--prefix=' + str(root / 'build/native-prefix'),
                    '--buildtype=debugoptimized',
                    '-Dudev_dir=' + str(root / 'build/native-prefix/lib/udev'),
                    '-Dintrospection=false', '-Dtests=disabled', '-Dprofiler=false',
                    '-Dinstalled_tests=false', '-Ddocs=false', '-Dbash_completion=false',
                    '-Dc_link_args=' + ' '.join('-L' + str(d / 'lib/x86_64-linux-gnu') for d in deps)] +
                   (['--reconfigure', '--clearcache'] if (build / 'build.ninja').exists() else []),
                   env=env, check=True)
subprocess.run(['ninja', '-C', str(build), '-j6'], env=env, check=True)
import json
installed = json.loads(subprocess.check_output(['meson', 'introspect', str(build), '--installed'], env=env))
for destination in installed.values():
    for target in destination if isinstance(destination, list) else [destination]:
        if not Path(target).is_relative_to(root / 'build/native-prefix'):
            raise RuntimeError('Refusing installation outside private prefix: ' + target)
subprocess.run(['meson', 'install', '-C', str(build), '--no-rebuild'], env=env, check=True)
subprocess.run([sys.executable, str(root / 'scripts/native-typelibs.py')], check=True)
