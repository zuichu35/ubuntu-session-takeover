#!/usr/bin/env python3
"""Build the verified mac-compatible GRD with a real /opt runtime prefix."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'build/grd-greeter-20261028'
PREFIX = Path('/opt/ubuntu-native-takeover-login-20261028/build/grd-prefix')
STAGE = WORK / 'stage'
SOURCE = ROOT / 'vendor/gnome-remote-desktop'
DEPS = ROOT / 'vendor/grd-deps/root/usr'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    patch_hashes = json.loads((ROOT / 'build/grd-patch-hashes.json').read_text())
    if any(sha(SOURCE / name) != expected for name, expected in patch_hashes.items()):
        raise RuntimeError('Patched GRD source differs from its recorded mac-compatible build')
    rdp = (SOURCE / 'src/grd-session-rdp.c').read_text()
    if ('freerdp_settings_set_bool (rdp_settings, FreeRDP_NlaSecurity, TRUE);' not in rdp or
            'if (g_getenv ("GRD_NATIVE_TAKEOVER"))\n    freerdp_settings_set_bool (rdp_settings, FreeRDP_ExtSecurity, FALSE);' not in rdp):
        raise RuntimeError('Mac HYBRID workaround or NLA authentication is missing')
    WORK.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env['PKG_CONFIG_PATH'] = ':'.join(str(DEPS / part) for part in
                                   ('lib/x86_64-linux-gnu/pkgconfig', 'share/pkgconfig'))
    env['PATH'] = str(DEPS / 'bin') + ':' + env['PATH']
    env['LD_LIBRARY_PATH'] = str(DEPS / 'lib/x86_64-linux-gnu')
    output = WORK / 'grd'
    with (WORK / 'build.log').open('w') as log:
        def run(args):
            subprocess.run([str(arg) for arg in args], env=env, stdout=log, stderr=log, check=True)
        run(['meson', 'setup', output, SOURCE, '--prefix=' + str(PREFIX),
             '--buildtype=release', '--libdir=lib/x86_64-linux-gnu', '--sysconfdir=/etc',
             '-Dman=false', '-Dtests=false', '-Dsystemd=false',
             '-Dsystemd_sysusers_dir=' + str(PREFIX / 'lib/sysusers.d'),
             '-Dsystemd_tmpfiles_dir=' + str(PREFIX / 'lib/tmpfiles.d'),
             '-Dc_link_args=-L' + str(DEPS / 'lib/x86_64-linux-gnu'),
             '-Dc_args=-ffile-prefix-map=' + str(ROOT) + '=.'] +
            (['--reconfigure'] if (output / 'build.ninja').exists() else []))
        run(['ninja', '-C', output, '-j4'])
        installed = json.loads(subprocess.check_output(['meson', 'introspect', output, '--installed'], env=env))
        for destinations in installed.values():
            for destination in destinations if isinstance(destinations, list) else [destinations]:
                if not Path(destination).is_relative_to(PREFIX):
                    raise RuntimeError('Refusing install outside candidate prefix: ' + destination)
        run(['meson', 'install', '-C', output, '--no-rebuild', '--destdir', STAGE])
    candidate = STAGE / PREFIX.relative_to('/')
    schemas = candidate / 'share/glib-2.0/schemas'
    subprocess.run(['glib-compile-schemas', schemas], check=True)
    config = (output / 'config.h').read_text()
    configured = dict(re.findall(r'^#define (GRD_\w+) "([^"]*)"', config, re.M))
    for key in ('GRD_DATA_DIR', 'GRD_LIBEXEC_DIR', 'GRD_DEFAULT_CONF', 'GRD_SHADER_DIR'):
        if not Path(configured[key]).is_relative_to(PREFIX):
            raise RuntimeError('Unexpected runtime resource prefix: ' + key)
    home_references = []
    elf_dependencies = {}
    runtime_env = dict(os.environ)
    runtime_env.pop('LD_LIBRARY_PATH', None)
    for path in candidate.rglob('*'):
        if not path.is_file() or path.is_symlink():
            continue
        raw = path.read_bytes()
        if b'/home/' in raw:
            home_references.append(str(path.relative_to(candidate)))
        if raw[:4] == b'\x7fELF':
            dynamic = subprocess.check_output(['readelf', '-d', path], text=True)
            ldd = subprocess.check_output(['ldd', path], env=runtime_env, text=True)
            if '/home/' in ldd or 'not found' in ldd or re.search(r'\((?:RPATH|RUNPATH)\).*?/home/', dynamic):
                raise RuntimeError('Private home dependency in installed ELF: ' + str(path))
            elf_dependencies[str(path.relative_to(candidate))] = ldd
    if home_references:
        raise RuntimeError('Home paths remain in installed resources: ' + ', '.join(home_references))
    result = {'prefix': str(PREFIX), 'stage': str(STAGE), 'installed_to_live_opt': False,
              'source_revision': subprocess.check_output(['git', '-C', SOURCE, 'rev-parse', 'HEAD'], text=True).strip(),
              'patch_hashes': patch_hashes, 'native_ext_security': False, 'nla_security': True,
              'configured_runtime_paths': configured, 'home_references': home_references,
              'private_home_dependencies': False,
              'files': {str(p.relative_to(candidate)): sha(p) for p in sorted(candidate.rglob('*'))
                        if p.is_file() and not p.is_symlink()},
              'elf_dependencies': elf_dependencies}
    (WORK / 'build-result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({key: result[key] for key in ('prefix', 'stage', 'home_references', 'private_home_dependencies')}))


if __name__ == '__main__':
    main()
