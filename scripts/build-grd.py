#!/usr/bin/env python3
"""Build patched GRD into this checkout's private prefix; no system installation."""
import argparse
import json
from pathlib import Path
import os
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PREFIX = ROOT / 'build/grd-prefix'


def validate_install_targets(installed, prefix):
    if prefix.resolve() != prefix.absolute():
        raise RuntimeError('Refusing symlink private prefix: ' + str(prefix))
    for values in installed.values():
        for value in values if isinstance(values, list) else [values]:
            target = Path(value)
            if not target.is_absolute() or not target.resolve().is_relative_to(prefix.resolve()):
                raise RuntimeError('Refusing installation outside private prefix: ' + value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true', help='Print build destinations; no writes')
    args = parser.parse_args()
    deps = ROOT / 'vendor/grd-deps/root/usr'
    source = ROOT / 'vendor/gnome-remote-desktop'
    build = ROOT / 'build/grd'
    options = ['--prefix=' + str(PREFIX), '-Dman=false', '-Dtests=false', '-Dsystemd=false',
               '-Dsystemd_sysusers_dir=' + str(PREFIX / 'lib/sysusers.d'),
               '-Dsystemd_tmpfiles_dir=' + str(PREFIX / 'lib/tmpfiles.d'),
               '-Dc_link_args=-L' + str(deps / 'lib/x86_64-linux-gnu')]
    if args.plan:
        print(json.dumps({'source': str(source), 'build': str(build), 'options': options,
                          'all_install_targets_checked': True}, indent=2))
        return
    for destination in (build, PREFIX):
        if destination.resolve() != destination:
            raise RuntimeError('Refusing symlink build destination: ' + str(destination))
    env = dict(os.environ)
    env['PKG_CONFIG_PATH'] = ':'.join(str(deps / p) for p in
                                    ('lib/x86_64-linux-gnu/pkgconfig', 'share/pkgconfig'))
    env['PATH'] = str(deps / 'bin') + ':' + env['PATH']
    env['LD_LIBRARY_PATH'] = str(deps / 'lib/x86_64-linux-gnu')
    subprocess.run(['meson', 'setup', str(build), str(source), *options] +
                   (['--reconfigure'] if (build / 'build.ninja').exists() else []), env=env, check=True)
    installed = json.loads(subprocess.check_output(
        ['meson', 'introspect', str(build), '--installed'], env=env))
    validate_install_targets(installed, PREFIX)
    subprocess.run(['ninja', '-C', str(build), '-j4'], env=env, check=True)
    subprocess.run(['meson', 'install', '-C', str(build), '--no-rebuild'], env=env, check=True)


if __name__ == '__main__':
    main()
