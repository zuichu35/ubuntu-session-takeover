#!/usr/bin/python3
"""Exercise production gateway early handover with real GRD/FreeRDP privately."""
import os
from pathlib import Path
import pwd
import shlex
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = ROOT / 'dist/3392-handover-fix-20261028/payload'
PREFIX = '/opt/ubuntu-native-takeover-login-20261028'
RESULTS = ROOT / 'artifacts/black-screen-20261028/gateway-live-protocol-tests'


def main():
    account = pwd.getpwuid(os.getuid()).pw_name
    for reject in (False, True):
        name = 'reject' if reject else 'accept'
        result = RESULTS / name
        result.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='native-gateway-' + name + '-') as runtime:
            environment = {
                'NATIVE_TEST_HOST_NET_NS': os.readlink('/proc/self/ns/net'),
                'NATIVE_TEST_RUNTIME_ROOT': PREFIX,
                'NATIVE_TEST_ARTIFACT_ROOT': str(result),
                'NATIVE_TEST_RUN_DIR': runtime,
                'LIBGL_ALWAYS_SOFTWARE': '1', 'GALLIUM_DRIVER': 'llvmpipe',
                '__EGL_VENDOR_LIBRARY_FILENAMES': '/usr/share/glvnd/egl_vendor.d/50_mesa.json',
            }
            commands = [
                ['mount', '--make-rprivate', '/'],
                ['mount', '--bind', str(PAYLOAD), PREFIX],
                ['mount', '-o', 'remount,bind,ro', PREFIX],
                ['mount', '-t', 'tmpfs', '-o', 'mode=755', 'tmpfs', '/dev/dri'],
            ]
            for device in Path('/dev').glob('nvidia*'):
                if not device.is_dir():
                    commands.append(['mount', '--bind', '/dev/null', str(device)])
            commands.append(['ip', 'link', 'set', 'lo', 'up'])
            probe = ['/usr/bin/python3', str(ROOT / 'tests/native_integration.py'), '--gateway-handover-probe']
            if reject:
                probe.append('--reject-handover')
            child = ['runuser', '-u', account, '--', 'env',
                     *(key + '=' + value for key, value in environment.items()), *probe]
            command = ' && '.join(shlex.join(args) for args in commands) + ' && exec ' + shlex.join(child)
            with (result / 'suite.log').open('w') as log:
                completed = subprocess.run(['sudo', '-n', 'unshare', '--mount', '--net', '--pid',
                    '--fork', '--mount-proc', '--kill-child', '/bin/bash', '-c', command],
                    cwd=ROOT, stdout=log, stderr=log, timeout=180)
            print(name + ': ' + str(completed.returncode) + ' (' + str(result / 'suite.log') + ')', flush=True)
            if completed.returncode:
                raise RuntimeError('Isolated gateway handover test failed; inspect its suite log')


if __name__ == '__main__':
    main()
