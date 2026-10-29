#!/usr/bin/env python3
"""Run GRD candidate tests with /home hidden in private mount/network namespaces."""
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'build/grd-greeter-20261028'
NAME = 'ubuntu-native-takeover-login-20261028'
PREFIX = Path('/opt') / NAME


def run_as_gdm():
    if os.geteuid() != 0:
        raise RuntimeError('--as-gdm requires root only to create isolated namespaces and switch test identity')
    account = pwd.getpwnam('gdm')
    results = WORK / 'gdm-home-hidden-results'
    results.mkdir(exist_ok=True)
    sandbox = Path(tempfile.mkdtemp(prefix='grd-gdm-isolated-'))
    sandbox.chmod(0o755)
    try:
        source = sandbox / 'source'
        for name in ('tests', 'takeover', 'vendor/ubuntu-freerdp'):
            shutil.copytree(ROOT / name, source / name, symlinks=True,
                            ignore=shutil.ignore_patterns('__pycache__'))
        isolated_opt = sandbox / 'opt'
        shutil.copytree(PREFIX, isolated_opt / NAME, symlinks=True)
        grd = isolated_opt / NAME / 'build/grd-prefix'
        shutil.rmtree(grd)
        shutil.copytree(WORK / 'stage/opt' / NAME / 'build/grd-prefix', grd, symlinks=True)
        for tree in (source, isolated_opt):
            for path in [tree, *tree.rglob('*')]:
                if not path.is_symlink():
                    path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
        for name in ('results', 'runtime', 'home'):
            path = sandbox / name
            path.mkdir(mode=0o700)
            os.chown(path, account.pw_uid, account.pw_gid)
        environment = {
            'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'C.UTF-8',
            'HOME': str(sandbox / 'home'), 'USER': 'gdm', 'LOGNAME': 'gdm',
            'NATIVE_TEST_HOST_NET_NS': os.readlink('/proc/self/ns/net'),
            'NATIVE_TEST_RUNTIME_ROOT': str(PREFIX),
            'NATIVE_TEST_ARTIFACT_ROOT': str(sandbox / 'results'),
            'NATIVE_TEST_RUN_DIR': str(sandbox / 'runtime'),
        }
        commands = [
            ['mount', '--make-rprivate', '/'],
            ['mount', '--bind', str(isolated_opt), '/opt'],
            ['mount', '-o', 'remount,bind,ro', '/opt'],
            ['mount', '-t', 'tmpfs', '-o', 'mode=755', 'tmpfs', '/home'],
            ['/usr/bin/python3', '-c', "from pathlib import Path; assert not any(Path('/home').iterdir())"],
            ['ip', 'link', 'set', 'lo', 'up'],
        ]
        probe = ['/usr/bin/python3', str(source / 'tests/native_integration.py'),
                 '--greeter-probe', '--handover-probe', '--negotiation-probe']
        tracing = '--trace-gdm' in sys.argv
        if tracing:
            # A non-root ptracer can inhibit setuid fusermount3. Use tracing
            # only for resource-startup evidence, not full clipboard acceptance.
            probe = ['strace', '-f', '-e', 'trace=openat,access,statx', '-o',
                     str(sandbox / 'results/filesystem.log'), *probe]
        # Only this test process receives render access, as logind grants to a
        # real physical greeter; no account membership or device ACL is changed.
        child = ['runuser', '-u', 'gdm', '-g', 'gdm', '-G', 'render', '--', 'env', '-i',
                 *(key + '=' + value for key, value in environment.items()),
                 '/bin/sh', '-c', 'id && exec ' + shlex.join(probe)]
        command = ' && '.join(shlex.join(args) for args in commands) + ' && cd / && exec ' + shlex.join(child)
        with (results / 'test.log').open('w') as log:
            completed = subprocess.run(['unshare', '--mount', '--net', '--pid', '--fork', '--mount-proc',
                                        '--kill-child', '/bin/bash', '-c', command],
                                       cwd='/', stdout=log, stderr=log, timeout=180)
        shutil.copytree(sandbox / 'results', results, dirs_exist_ok=True)
        report = {'actual_uid': account.pw_uid, 'actual_primary_gid': account.pw_gid,
                  'temporary_supplementary_group': 'render', 'account_membership_modified': False,
                  'private_mount_network_pid_namespaces': True, 'home_hidden': True,
                  'headless_test_mode': True, 'synthetic_credentials': True,
                  'traced': tracing,
                  'real_gdm_pam_tested': False, 'returncode': completed.returncode}
        (results / 'namespace.json').write_text(json.dumps(report, indent=2) + '\n')
        for path in [results, *results.rglob('*')]:
            if not path.is_symlink():
                path.chmod(0o755 if path.is_dir() else 0o644)
        if completed.returncode:
            raise RuntimeError('Actual-GDM-UID isolated test failed; inspect ' + str(results / 'test.log'))
        grd_log = (results / 'native-evidence/grd.log').read_text()
        if 'Initialization of CUDA was successful' not in grd_log or 'RDP server started' not in grd_log:
            raise RuntimeError('GDM test did not establish CUDA/PTX and RDP readiness')
        print(json.dumps({'results': str(results), 'actual_uid': account.pw_uid,
                          'home_hidden': True, 'cuda_and_rdp_started': True, 'returncode': 0}))
    finally:
        shutil.rmtree(sandbox)


def main():
    if '--as-gdm' in sys.argv:
        run_as_gdm()
        return
    candidate = WORK / 'stage/opt' / NAME / 'build/grd-prefix'
    test_opt = WORK / 'test-opt'
    if not test_opt.exists():
        shutil.copytree(PREFIX, test_opt / NAME, symlinks=True)
    target = test_opt / NAME / 'build/grd-prefix'
    if target.exists():
        shutil.rmtree(target)  # Only our disposable, never-live test copy.
    shutil.copytree(candidate, target, symlinks=True)
    for reject in (False, True):
        results = WORK / ('home-hidden-reject-results' if reject else 'home-hidden-results')
        results.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='grd-greeter-ns-') as temporary:
            directory = Path(temporary)
            for name in ('source', 'results', 'home'):
                (directory / name).mkdir()
            env = dict(os.environ,
                NATIVE_TEST_HOST_NET_NS=os.readlink('/proc/self/ns/net'),
                NATIVE_TEST_RUNTIME_ROOT=str(PREFIX),
                NATIVE_TEST_ARTIFACT_ROOT=str(directory / 'results'),
                NATIVE_TEST_RUN_DIR=str(directory / 'runtime'),
                HOME=str(directory / 'home'))
            commands = [
                ['mount', '--make-rprivate', '/'],
                ['mount', '--bind', str(ROOT), str(directory / 'source')],
                ['mount', '--bind', str(results), str(directory / 'results')],
                ['mount', '--bind', str(test_opt), '/opt'],
                ['mount', '-t', 'tmpfs', '-o', 'mode=755', 'tmpfs', '/home'],
                ['/usr/bin/python3', '-c', "from pathlib import Path; assert not any(Path('/home').iterdir())"],
                ['ip', 'link', 'set', 'lo', 'up'],
            ]
            command = ' && '.join(shlex.join(args) for args in commands)
            probe = ['strace', '-f', '-e', 'trace=openat,access,statx', '-o', str(directory / 'results/filesystem.log'),
                     '/usr/bin/python3', str(directory / 'source/tests/native_integration.py'),
                     '--greeter-probe', '--handover-probe', '--negotiation-probe']
            if reject:
                probe.append('--reject-handover')
            command += ' && cd / && exec ' + shlex.join(probe)
            with (results / 'test.log').open('w') as log:
                result = subprocess.run(['unshare', '--user', '--map-root-user', '--mount', '--net',
                                         '/bin/bash', '-c', command], env=env, cwd='/', stdout=log, stderr=log)
            namespace = {'home_hidden': True, 'private_mount_namespace': True,
                         'private_network_namespace': True, 'headless_test_mode': True,
                         'synthetic_credentials': True, 'real_gdm_pam_tested': False,
                         'test_uid': 'mapped current user as namespace root; not real GDM',
                         'returncode': result.returncode}
            (results / 'namespace.json').write_text(json.dumps(namespace, indent=2) + '\n')
            if result.returncode:
                raise RuntimeError('Isolated GRD test failed; inspect ' + str(results / 'test.log'))
        print(json.dumps({'results': str(results), 'home_hidden': True, 'returncode': 0}))


if __name__ == '__main__':
    main()
