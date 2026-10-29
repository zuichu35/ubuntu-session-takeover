#!/usr/bin/env python3
"""Attempt to load private Mutter; system fingerprints are diagnostic only.

Preserves the distribution's original ExecStart, including existing NX wrappers.
An OS update does not disable takeover. Loader/runtime errors remain visible
in the GNOME Shell service journal and preserve its normal failure status.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'build/native-system-abi.json'


def mismatches():
    try:
        saved = json.loads(MANIFEST.read_text())
    except (OSError, ValueError):
        return ['Missing or unreadable compatibility manifest: ' + str(MANIFEST)]
    if not isinstance(saved, dict) or not saved:
        return ['Invalid or empty compatibility manifest: ' + str(MANIFEST)]
    changed = []
    for name, expected in saved.items():
        try:
            actual = hashlib.sha256(Path(name).read_bytes()).hexdigest()
        except OSError:
            actual = None
        if actual != expected:
            changed.append(name)
    return changed


def main(args=None):
    args = list(sys.argv[1:] if args is None else args)
    changed = mismatches()
    ok = not changed
    if args == ['--check']:
        print(json.dumps({'native_build_matches_system': ok, 'mismatched_files': changed,
                          'startup_policy': 'attempt-load', 'hash_mismatch_blocks_startup': False}))
        return
    if args[:1] == ['--']:
        args.pop(0)
    if not args:
        raise SystemExit('Original GNOME Shell command required')
    env = dict(os.environ)
    env.pop('MUTTER_NATIVE_TAKEOVER_TEST', None)
    env.pop('MUTTER_NATIVE_TEST_AUTH_HELPER', None)
    env.pop('MUTTER_NATIVE_BOOT_PRIVACY', None)
    env.pop('MUTTER_NATIVE_GREETER', None)
    sys.path.insert(0, str(ROOT))
    from takeover.login_gateway import boot_guard_present
    from takeover.greeter_identity import find_native_greeter_session, is_gdm_group_member
    # These hints only select bounded retries/refusal. They never confer the
    # greeter role; each successful identification still requires logind.
    expects_native_greeter = ('--mode=gdm' in args and
        (env.get('GDM_SEAT_ID') == 'seat0' or env.get('XDG_SEAT') == 'seat0') and
        is_gdm_group_member(os.getuid()))
    greeter_session = None
    for attempt in range(3 if expects_native_greeter else 1):
        greeter_session = find_native_greeter_session()
        if greeter_session is not None:
            break
        if expects_native_greeter and attempt < 2:
            print('Native takeover: waiting for logind to confirm the physical GDM greeter '
                  f'(attempt {attempt + 1}/3).', file=sys.stderr, flush=True)
            time.sleep(.25)
    if expects_native_greeter and greeter_session is None:
        raise SystemExit('Native takeover: cannot confirm the physical GDM greeter after 3 attempts; '
                         'refusing to launch it with the ordinary desktop role.')
    if greeter_session is not None:
        env['MUTTER_NATIVE_GREETER'] = '1'
    elif boot_guard_present():
        env['MUTTER_NATIVE_BOOT_PRIVACY'] = '1'
    if not ok:
        print('Native takeover: system fingerprints changed; continuing with the private build. '
              'Diagnostic differences: ' + ', '.join(changed), file=sys.stderr, flush=True)
    lib = ROOT / 'build/native-prefix/lib/x86_64-linux-gnu'
    env['LD_LIBRARY_PATH'] = f'{lib}:{lib}/mutter-18' + (':' + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
    env['MUTTER_NATIVE_TAKEOVER'] = '1'
    env['MUTTER_NATIVE_LOCAL_AUTH_HELPER'] = str(ROOT / 'build/native-prefix/libexec/native-local-auth')
    env['GI_TYPELIB_PATH'] = str(lib / 'mutter-18') + (':' + env['GI_TYPELIB_PATH'] if env.get('GI_TYPELIB_PATH') else '')
    env['MUTTER_NATIVE_TYPELIB_DIR'] = str(lib / 'mutter-18')
    if env.get('MUTTER_NATIVE_BOOT_PRIVACY') == '1':
        # A new Shell ABI could select a system Mutter despite LD_LIBRARY_PATH.
        # That is an actual missing privacy capability, not a hash mismatch.
        import subprocess
        shell = '/usr/bin/gnome-shell'
        linkage = subprocess.run(['ldd', shell], env=env, capture_output=True, text=True)
        if linkage.returncode or str(lib / 'libmutter-18.so.0') not in linkage.stdout:
            raise SystemExit('Cannot guarantee startup privacy: GNOME Shell does not load the private Mutter')
    try:
        os.execvpe(args[0], args, env)
    except OSError as error:
        raise SystemExit(f'Native takeover: failed to execute {args[0]}: {error}') from error


if __name__ == '__main__':
    main()
