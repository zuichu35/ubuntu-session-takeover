#!/usr/bin/python3
"""Build and validate the pinned runtime inside an unprivileged CI container.

No host session or system bus is used. This is not an installation command.
A passing receipt is emitted only after every target case has really passed.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'build/target-ci'
PROFILE = 'ubuntu-26.04-amd64-gnome-50.1-updates-20261029'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_verified_receipt(path, receipt, profile, facts, inspect):
    from takeover.compatibility import evaluate
    report = evaluate(profile, dict(facts, receipt=receipt))
    if report['status'] != 'verified':
        raise RuntimeError('Candidate cannot receive a validation receipt: ' + '; '.join(report['reasons']))
    path.write_text(json.dumps(receipt, indent=2) + '\n')
    try:
        report = inspect()
        if report['status'] != 'verified':
            raise RuntimeError('Written receipt failed final verification: ' + '; '.join(report['reasons']))
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main():
    if os.geteuid() == 0 or not Path('/.dockerenv').is_file():
        raise SystemExit('Run this target job as an ordinary user in the dedicated CI container')
    host_namespace = os.environ.get('NATIVE_TEST_HOST_NET_NS')
    if not host_namespace or os.readlink('/proc/self/ns/net') == host_namespace:
        raise SystemExit('A separate network namespace and its actual host namespace ID are required')
    if Path('/run/dbus/system_bus_socket').exists() or list(Path('/dev/dri').glob('*')):
        raise SystemExit('Host system bus and physical DRM devices must be absent')
    if list(Path('/dev').glob('nvidia*')):
        raise SystemExit('Physical NVIDIA devices must be absent')
    if subprocess.check_output(['/usr/bin/gnome-shell', '--version'], text=True).strip() != 'GNOME Shell 50.1':
        raise SystemExit('needs-validation: the installed GNOME Shell does not match the 50.1 target profile')
    os.chdir(ROOT)
    WORK.mkdir(parents=True, exist_ok=True)
    logs = WORK / 'logs'
    logs.mkdir(exist_ok=True)
    receipt_path = ROOT / 'build/compatibility-validation.json'
    receipt_path.unlink(missing_ok=True)
    environment = dict(os.environ, LIBGL_ALWAYS_SOFTWARE='1', GALLIUM_DRIVER='llvmpipe',
        GSK_RENDERER='cairo', __EGL_VENDOR_LIBRARY_FILENAMES='/usr/share/glvnd/egl_vendor.d/50_mesa.json',
        DBUS_SYSTEM_BUS_ADDRESS='unix:path=/nonexistent-native-ci-system-bus')
    for name in ('DBUS_SESSION_BUS_ADDRESS', 'DISPLAY', 'WAYLAND_DISPLAY', 'SESSION_MANAGER'):
        environment.pop(name, None)

    def run(name, command, *, extra=None, timeout=1800):
        print('Running ' + name, flush=True)
        with (logs / (name + '.log')).open('w') as log:
            result = subprocess.run(command, cwd=ROOT, env=dict(environment, **(extra or {})),
                                    stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        if result.returncode:
            print((logs / (name + '.log')).read_text()[-36000:], file=sys.stderr)
            raise RuntimeError(name + ' failed; no passing receipt emitted')

    run('fetch', ['/usr/bin/python3', 'scripts/fetch-upstreams.py'])
    run('patch-native', ['/usr/bin/python3', 'scripts/patch-native.py'])
    run('patch-grd', ['/usr/bin/python3', 'scripts/patch-grd.py'])
    run('build-native', ['/usr/bin/python3', 'scripts/build-native.py'])
    run('build-grd', ['/usr/bin/python3', 'scripts/build-grd.py'])
    run('record-manifest', ['/usr/bin/python3', 'scripts/record-build-compatibility.py',
        '--runtime-root', str(ROOT), '--source-root', str(ROOT), '--profile', PROFILE,
        '--development-owner'])
    manifest_path = ROOT / 'build/compatibility-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    profile = json.loads((ROOT / 'compat/profiles' / (PROFILE + '.json')).read_text())
    manifest_hash = digest(manifest_path)
    sys.path.insert(0, str(ROOT))
    from takeover.compatibility import collect_snapshot, inspect_candidate

    def verify_inventory():
        facts = collect_snapshot(ROOT, profile, required_owner=os.getuid(),
                                 profile_sha256=manifest['profile_sha256'])
        if facts['errors'] or not manifest.get('private_inventory') or \
                facts.get('private_inventory') != manifest['private_inventory']:
            raise RuntimeError('Complete private runtime inventory changed during validation: ' +
                               '; '.join(facts['errors']))
        if not manifest.get('shell_linkage') or facts.get('shell_linkage') != manifest['shell_linkage']:
            raise RuntimeError('Target Shell ELF linkage changed during validation')

    verify_inventory()
    tested_hashes = {
        **{'runtime:' + name: digest(ROOT / name) for name in profile['tested_runtime']},
        **{'system:' + name: digest(name) for name in profile['tested_system']}}
    expected_hashes = {
        **{'runtime:' + name: manifest['artifacts'][name] for name in profile['tested_runtime']},
        **{'system:' + name: manifest['system_hashes'][name] for name in profile['tested_system']}}
    if tested_hashes != expected_hashes:
        raise RuntimeError('Actual test inputs do not match the build manifest')
    helper = subprocess.run([str(ROOT / 'build/native-prefix/libexec/native-local-auth')],
        input=b'', capture_output=True, env=environment, timeout=5)
    if helper.returncode != 2 or helper.stdout or helper.stderr:
        raise RuntimeError('Actual PAM helper failed the empty-input rejection boundary')
    freerdp = shutil.which('xfreerdp') or shutil.which('xfreerdp3')
    if not freerdp:
        raise RuntimeError('Target FreeRDP X11 client is missing')
    evidence = {}
    scenarios = [('startup', ['tests/native_startup_lock.py']),
                 ('shell', ['tests/native_integration.py', '--shell', '--boot-guard']),
                 ('desktop', ['tests/native_integration.py']),
                 ('gateway', ['tests/native_integration.py', '--gateway-handover-probe']),
                 ('gateway-reject', ['tests/native_integration.py', '--gateway-handover-probe', '--reject-handover'])]
    for name, arguments in scenarios:
        directory = WORK / name
        directory.mkdir(exist_ok=True)
        run(name, ['/usr/bin/python3', *arguments], timeout=240, extra={
            'NATIVE_TEST_RUNTIME_ROOT': str(ROOT), 'NATIVE_TEST_ARTIFACT_ROOT': str(directory),
            'NATIVE_TEST_RUN_DIR': str(directory / 'runtime'), 'NATIVE_TEST_FREERDP': freerdp})
        evidence[name] = {str(path.relative_to(ROOT)): digest(path)
                          for path in directory.rglob('*result.json')}
        if not evidence[name]:
            raise RuntimeError(name + ' produced no result evidence')
        verify_inventory()
    shell = json.loads((WORK / 'shell/native-shell-integration-result.json').read_text())
    startup = json.loads((WORK / 'startup/startup-lock/result.json').read_text())
    gateway = json.loads((WORK / 'gateway/greeter-integration-result.json').read_text())
    rejection = json.loads((WORK / 'gateway-reject/greeter-auth-rejection-result.json').read_text())
    assert shell['startup_guard_verified'] and shell['rdp_input_to_original_app']
    assert shell['verified_local_reclaim_revokes_remote_without_second_password']
    assert startup['failed_reclaim']['wrong_password_preserves_privacy']
    assert startup['late_callback']['late_lock_callbacks_cannot_reactivate_native_privacy_after_local_auth']
    assert gateway['automatic_rdp_handover_verified'] and gateway['fresh_client_handshake_waited_for_target']
    assert rejection['rdstls_wrong_password_rejected']
    # The real Shell process must have loaded our private libraries, not the
    # system Mutter that happens to share its ABI. The harness reads its maps.
    for relative in profile['tested_runtime']:
        if '/libmutter' in relative:
            absolute = str((ROOT / relative).resolve())
            if shell['loaded_files'].get(absolute) != tested_hashes['runtime:' + relative]:
                raise RuntimeError('Real Shell did not load the tested private library: ' + relative)
    for absolute in ('/usr/bin/gnome-shell', '/usr/lib/gnome-shell/libshell-18.so'):
        if shell['loaded_files'].get(str(Path(absolute).resolve())) != tested_hashes['system:' + absolute]:
            raise RuntimeError('Real target Shell file was not loaded: ' + absolute)
    if digest(manifest_path) != manifest_hash:
        raise RuntimeError('Build manifest changed during validation')
    verify_inventory()
    for relative, expected in manifest['artifacts'].items():
        if digest(ROOT / relative) != expected:
            raise RuntimeError('Runtime changed during validation: ' + relative)
    for absolute in profile['tested_system']:
        if digest(absolute) != tested_hashes['system:' + absolute]:
            raise RuntimeError('System target changed during validation: ' + absolute)
    receipt = {'schema': 1, 'scope': 'target-shell-integration', 'profile_id': PROFILE,
        'manifest_sha256': manifest_hash, 'target': manifest['built_for'],
        'cases': {name: 'passed' for name in ('target-shell-startup', 'desktop-rdp',
                 'startup-dual-lock', 'local-auth-reclaim', 'gateway-handover')},
        'evidence': evidence, 'tested_binaries': tested_hashes,
        'limitations': ['Headless virtual outputs and CPU rendering',
            'Private fake logind/GDM; real GNOME Shell ScreenShield',
            'Test authentication helper; real PAM and GDM cold boot unverified',
            'No physical GPU/display/input or Windows/macOS client acceptance']}
    facts = collect_snapshot(ROOT, profile, required_owner=os.getuid(),
                             profile_sha256=manifest['profile_sha256'])
    write_verified_receipt(receipt_path, receipt, profile, facts,
        lambda: inspect_candidate(ROOT, required_owner=os.getuid(), profile_id=PROFILE))
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
