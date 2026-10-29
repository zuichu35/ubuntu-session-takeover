#!/usr/bin/python3
"""Private-bus/CPU-rendered native startup and ordinary RDP regression suite."""
import os
import pwd
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'build/native-startup-lock-20261028'
PREFIX = '/opt/ubuntu-native-takeover-login-20261028'


def main():
    env = dict(os.environ, NATIVE_TEST_HOST_NET_NS=os.readlink('/proc/self/ns/net'),
               NATIVE_TEST_RUNTIME_ROOT=PREFIX, LIBGL_ALWAYS_SOFTWARE='1', GALLIUM_DRIVER='llvmpipe',
               __EGL_VENDOR_LIBRARY_FILENAMES='/usr/share/glvnd/egl_vendor.d/50_mesa.json')
    mounts = [
        ['mount', '--make-rprivate', '/'],
        ['mount', '--bind', WORK / 'test-opt', '/opt'],
        ['mount', '-o', 'remount,bind,ro', '/opt'],
        ['mount', '-t', 'tmpfs', '-o', 'mode=755', 'tmpfs', '/dev/dri'],
    ]
    for path in Path('/dev').glob('nvidia*'):
        if not path.is_dir():
            mounts.append(['mount', '--bind', '/dev/null', path])
    mounts.append(['ip', 'link', 'set', 'lo', 'up'])
    prefix = ' && '.join(shlex.join([str(v) for v in args]) for args in mounts)
    scenarios = [
        ('startup', ['tests/native_startup_lock.py']),
        ('desktop', ['tests/native_integration.py']),
        ('greeter', ['tests/native_integration.py', '--greeter-probe', '--handover-probe']),
    ]
    for name, args in scenarios:
        result = WORK / 'test-results' / name
        result.mkdir(parents=True, exist_ok=True)
        scenario = dict(env, NATIVE_TEST_ARTIFACT_ROOT=str(result),
                        NATIVE_TEST_RUN_DIR='/tmp/native-startup-suite-' + name)
        account = pwd.getpwuid(os.getuid()).pw_name
        exported = {key: scenario[key] for key in (
            'NATIVE_TEST_HOST_NET_NS', 'NATIVE_TEST_RUNTIME_ROOT', 'NATIVE_TEST_ARTIFACT_ROOT',
            'NATIVE_TEST_RUN_DIR', 'LIBGL_ALWAYS_SOFTWARE', 'GALLIUM_DRIVER', '__EGL_VENDOR_LIBRARY_FILENAMES')}
        command = prefix + ' && exec ' + shlex.join(['runuser', '-u', account, '--', 'env',
            *(key + '=' + value for key, value in exported.items()),
            '/usr/bin/python3', str(ROOT / args[0]), *args[1:]])
        with (result / 'suite.log').open('w') as log:
            completed = subprocess.run(['sudo', '-n', 'unshare', '--mount', '--net',
                                        '--pid', '--fork', '--mount-proc', '--kill-child', '/bin/bash', '-c', command],
                                       cwd=ROOT, env=scenario, stdout=log, stderr=log, timeout=180)
        print(name + ': ' + str(completed.returncode), flush=True)
        if completed.returncode:
            raise RuntimeError('Isolated test failed: ' + str(result / 'suite.log'))


if __name__ == '__main__':
    main()
