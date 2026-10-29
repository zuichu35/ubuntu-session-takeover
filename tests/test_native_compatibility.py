import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from takeover import startup_policy as policy

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def safe_context(**changes):
    return {'known': True, 'guard': 'absent', 'owners': [],
            'session': '/org/freedesktop/login1/session/test', 'role': 'desktop', **changes}


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir(mode=0o700)
        self.launcher = module('native_shell_test', ROOT / 'scripts/native-shell.py')
        self.context = safe_context()
        self.report = {'schema': 1, 'status': 'verified', 'paths': {}, 'reasons': []}
        self.private = {'LD_LIBRARY_PATH': '/test/native-prefix/lib:/test/native-prefix/lib/mutter-99',
                        'GI_TYPELIB_PATH': '/test/native-prefix/lib/mutter-99',
                        'MUTTER_NATIVE_TYPELIB_DIR': '/test/native-prefix/lib/mutter-99',
                        'MUTTER_NATIVE_LOCAL_AUTH_HELPER': '/test/native-prefix/libexec/native-local-auth'}
        for target, value in [(policy, ('RUNTIME', self.runtime / 'ubuntu-native-takeover')),
                              (policy, ('GUARD', self.root / 'missing-guard'))]:
            self.enterContext(patch.object(target, *value))
        self.inspect = self.enterContext(patch.object(policy, 'inspect_startup', return_value=self.context))
        self.enterContext(patch.object(policy, 'guard_state', side_effect=lambda: self.context['guard']))
        self.enterContext(patch.object(self.launcher, 'candidate_report', side_effect=lambda: self.report))
        self.enterContext(patch.object(self.launcher, 'private_environment', side_effect=lambda report: dict(self.private)))

    def execute(self, args=None, **environment):
        args = args or ['--', '/original-wrapper', '/usr/bin/gnome-shell', '--mode=ubuntu']
        with patch.object(self.launcher.os, 'execvpe') as execute, \
             patch.dict(os.environ, environment), contextlib.redirect_stderr(io.StringIO()) as log:
            self.launcher.main(args)
        return execute.call_args.args, log.getvalue()

    def test_verified_private_preserves_wrapper_and_uses_report_abi(self):
        (command, argv, env), _ = self.execute(MUTTER_NATIVE_TAKEOVER_TEST='1',
            MUTTER_NATIVE_TEST_AUTH_HELPER='/test-only', MUTTER_NATIVE_GREETER='1')
        self.assertEqual(command, '/original-wrapper')
        self.assertEqual(argv, ['/original-wrapper', '/usr/bin/gnome-shell', '--mode=ubuntu'])
        self.assertEqual(env['MUTTER_NATIVE_TAKEOVER'], '1')
        self.assertEqual(env['UBUNTU_NATIVE_SESSION_MODE'], 'private')
        self.assertEqual(env['MUTTER_NATIVE_TYPELIB_DIR'], self.private['GI_TYPELIB_PATH'])
        self.assertNotIn('MUTTER_NATIVE_TAKEOVER_TEST', env)
        self.assertNotIn('MUTTER_NATIVE_TEST_AUTH_HELPER', env)
        self.assertNotIn('MUTTER_NATIVE_GREETER', env)

    def test_missing_or_changed_validation_falls_back_only_when_safe(self):
        for status in ('incompatible', 'needs-validation', 'unknown'):
            self.report = {'status': status, 'paths': {}, 'reasons': ['validation unavailable']}
            (command, argv, env), log = self.execute(
                LD_LIBRARY_PATH=str(ROOT / 'build/native-prefix/lib/mutter-99') + ':/opt/wrapper/lib',
                GI_TYPELIB_PATH='/opt/ubuntu-native-takeover-old/lib/mutter-42:/opt/wrapper/typelibs',
                LD_PRELOAD='/opt/ubuntu-native-takeover-old/lib/private.so:/opt/wrapper/helper.so',
                MUTTER_NATIVE_BOOT_PRIVACY='1', MUTTER_NATIVE_TAKEOVER='1')
            self.assertEqual(env['UBUNTU_NATIVE_SESSION_MODE'], 'system-fallback')
            self.assertFalse(any(k.startswith('MUTTER_NATIVE_') for k in env))
            self.assertEqual(env['LD_LIBRARY_PATH'], '/opt/wrapper/lib')
            self.assertEqual(env['GI_TYPELIB_PATH'], '/opt/wrapper/typelibs')
            self.assertEqual(env['LD_PRELOAD'], '/opt/wrapper/helper.so')
            self.assertEqual(argv[1:], ['/usr/bin/gnome-shell', '--mode=ubuntu'])
            self.assertIn('native backend disabled', log)

    def test_guard_requires_verified_candidate_and_keeps_boot_privacy(self):
        self.context['guard'] = 'present'
        (_, _, env), _ = self.execute()
        self.assertEqual(env['MUTTER_NATIVE_BOOT_PRIVACY'], '1')
        self.report['status'] = 'needs-validation'
        with patch.object(self.launcher.os, 'execvpe') as execute:
            with self.assertRaisesRegex(SystemExit, 'TTY or SSH'):
                self.launcher.main(['/usr/bin/gnome-shell'])
            execute.assert_not_called()

    def test_unknown_state_and_existing_compositor_never_exec_either_mode(self):
        for status in ('verified', 'incompatible'):
            self.report['status'] = status
            for update in ({'known': False}, {'guard': 'unknown'},
                           {'owners': ['org.gnome.Shell']}, {'owners': ['org.gnome.Mutter.NativeTakeover']}):
                self.context.clear()
                self.context.update(safe_context(**update))
                with patch.object(self.launcher.os, 'execvpe') as execute:
                    with self.assertRaises(SystemExit):
                        self.launcher.main(['/usr/bin/gnome-shell'])
                    execute.assert_not_called()

    def test_guard_or_owner_change_during_verification_prevents_exec(self):
        for changed in (safe_context(owners=['org.gnome.Shell']), safe_context(session='/other')):
            self.inspect.side_effect = [safe_context(), changed]
            with patch.object(self.launcher.os, 'execvpe') as execute:
                with self.assertRaises(SystemExit):
                    self.launcher.main(['/usr/bin/gnome-shell'])
                execute.assert_not_called()
        self.inspect.side_effect = None
        with patch.object(policy, 'guard_state', return_value='present'), \
             patch.object(self.launcher.os, 'execvpe') as execute:
            with self.assertRaisesRegex(SystemExit, 'immediately before exec'):
                self.launcher.main(['/usr/bin/gnome-shell'])
            execute.assert_not_called()

    def test_role_comes_from_logind_and_not_the_mode_argument(self):
        with patch.object(policy, 'is_gdm_group_member', return_value=False):
            (_, _, env), _ = self.execute(['/usr/bin/gnome-shell', '--mode=gdm'], MUTTER_NATIVE_GREETER='1')
        self.assertNotIn('MUTTER_NATIVE_GREETER', env)
        self.context.update(role='greeter', guard='present')
        (_, _, env), _ = self.execute()
        self.assertEqual(env['MUTTER_NATIVE_GREETER'], '1')
        self.assertNotIn('MUTTER_NATIVE_BOOT_PRIVACY', env)

    def test_greeter_waits_for_actual_logind_session(self):
        self.context['role'] = 'greeter'
        self.inspect.side_effect = [safe_context(known=False, session=None), self.context, self.context]
        with patch.object(policy, 'is_gdm_group_member', return_value=True), \
             patch.object(self.launcher.time, 'sleep') as sleep:
            (_, _, env), _ = self.execute(['/usr/bin/gnome-shell', '--mode=gdm'])
        sleep.assert_called_once_with(.25)
        self.assertEqual(env['MUTTER_NATIVE_GREETER'], '1')

    def test_check_only_reports_candidate_without_bus_or_runtime_writes(self):
        with patch.object(policy, 'acquire_launch_lock') as lock, contextlib.redirect_stdout(io.StringIO()) as out:
            self.launcher.main(['--check'])
        report = json.loads(out.getvalue())
        self.assertEqual(report['status'], 'verified')
        self.assertTrue(report['artifact_hash_mismatch_blocks_private_startup'])
        self.assertTrue(report['system_hash_change_is_diagnostic'])
        lock.assert_not_called()
        self.inspect.assert_not_called()

    def test_relative_and_symlink_private_paths_are_removed_without_dropping_wrapper(self):
        alias = self.root / 'private-alias'
        alias.symlink_to(ROOT / 'build/native-prefix/lib/mutter-77')
        environment = policy.clean_environment({'LD_LIBRARY_PATH':
            'build/native-prefix/lib/mutter-66:' + str(alias) + ':/opt/wrapper/lib'}, ROOT, self.report)
        self.assertEqual(environment['LD_LIBRARY_PATH'], '/opt/wrapper/lib')

    def test_exec_failure_is_visible_and_does_not_retry_system_shell(self):
        with patch.object(self.launcher.os, 'execvpe', side_effect=FileNotFoundError('missing')) as execute:
            with self.assertRaisesRegex(SystemExit, 'Failed to execute original wrapper'):
                self.launcher.main(['/missing/wrapper'])
        execute.assert_called_once()

    def test_real_exec_preserves_wrapper_arguments_environment_and_exit_status(self):
        loader = r'''
import importlib.util,json,pathlib,sys
s=importlib.util.spec_from_file_location('launcher',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
mode=sys.argv[3];m.policy.RUNTIME=pathlib.Path(sys.argv[2])/'ubuntu-native-takeover'
context={'known':True,'guard':'absent','owners':[],'session':'/test/session','role':'desktop'}
m.policy.inspect_startup=lambda:dict(context);m.policy.guard_state=lambda:'absent'
m.candidate_report=lambda:{'status':'verified' if mode=='private' else 'needs-validation','paths':{},'reasons':[]}
m.private_environment=lambda report:{'LD_LIBRARY_PATH':str(pathlib.Path(sys.argv[2])/'native-prefix/lib'),'GI_TYPELIB_PATH':str(pathlib.Path(sys.argv[2])/'native-prefix/lib/mutter-99')}
m.main(sys.argv[4:])
'''
        wrapper = self.root / 'wrapper.py'
        wrapper.write_text('import os,sys\nos.execv(sys.argv[1],sys.argv[1:])\n')
        child = ("import json,os,sys;print(json.dumps({'args':sys.argv[1:],'pid':os.getpid(),"
                 "'mode':os.getenv('UBUNTU_NATIVE_SESSION_MODE'),'native':os.getenv('MUTTER_NATIVE_TAKEOVER'),"
                 "'test':os.getenv('MUTTER_NATIVE_TAKEOVER_TEST'),'ld':os.getenv('LD_LIBRARY_PATH')}));sys.exit(7)")
        for mode in ('private', 'system-fallback'):
            env = dict(os.environ, MUTTER_NATIVE_TAKEOVER_TEST='1',
                       LD_LIBRARY_PATH=str(ROOT / 'build/native-prefix/lib/mutter-77') + ':/usr/lib')
            result = subprocess.run(['/usr/bin/python3', '-c', loader, str(ROOT / 'scripts/native-shell.py'),
                str(self.runtime), mode, '--', '/usr/bin/python3', str(wrapper), '/usr/bin/python3',
                '-c', child, 'argument with spaces', '--mode=ubuntu'], env=env,
                capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 7, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report['args'], ['argument with spaces', '--mode=ubuntu'])
            self.assertEqual(report['mode'], mode)
            self.assertEqual(report['native'], '1' if mode == 'private' else None)
            self.assertIsNone(report['test'])
            if mode == 'system-fallback':
                self.assertEqual(report['ld'], '/usr/lib')
            receipt = json.loads((policy.RUNTIME / policy.RECEIPT).read_text())
            self.assertEqual(receipt['pid'], report['pid'])
            self.assertEqual(receipt['mode'], mode)


class StartupEvidenceTests(unittest.TestCase):
    def test_logind_metadata_and_nss_must_both_confirm_the_real_role(self):
        uid = os.getuid()
        sessions = [('s1', uid, 'untrusted-name', 'seat0', '/session/real')]
        base = {'User': (uid, '/user'), 'Seat': ('seat0', '/seat'), 'Type': 'wayland',
                'Remote': False, 'State': 'active', 'Class': 'user'}
        props = dict(base)
        def call(bus, name, path, interface, method, parameters=None):
            return sessions if method == 'ListSessions' else props
        with patch.object(policy, '_connect'), patch.object(policy, '_call', side_effect=call), \
             patch.object(policy, 'is_gdm_group_member', return_value=False) as group:
            self.assertEqual(policy.current_session(), {'session': '/session/real', 'role': 'desktop'})
            for changed in ({'Remote': True}, {'State': 'closing'}, {'User': (uid + 1, '/other')},
                            {'Seat': ('seat1', '/other')}, {'Type': 'x11'}, {'Class': 'greeter'}):
                props.clear()
                props.update(base, **changed)
                with self.assertRaises(policy.StartupRefused):
                    policy.current_session()
            props.update(base, Class='greeter')
            group.return_value = True
            self.assertEqual(policy.current_session()['role'], 'greeter')
            sessions.append(('s2', uid, 'another-name', 'seat0', '/session/duplicate'))
            with self.assertRaises(policy.StartupRefused):
                policy.current_session()

    def test_candidate_inspection_cannot_relax_root_ownership_from_environment(self):
        launcher = module('owner_checked_launcher', ROOT / 'scripts/native-shell.py')
        inspect = Mock(return_value={'status': 'needs-validation'})
        with patch.dict(sys.modules, {'takeover.compatibility': SimpleNamespace(inspect_candidate=inspect)}), \
             patch.dict(os.environ, NATIVE_REQUIRED_OWNER=str(os.getuid())):
            self.assertEqual(launcher.candidate_report()['status'], 'needs-validation')
        inspect.assert_called_once_with(ROOT, required_owner=0)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.enterContext(patch.object(policy, 'RUNTIME', self.root / 'ubuntu-native-takeover'))
        self.enterContext(patch.object(policy, 'GUARD', self.root / 'guard'))
        self.guard_check = policy.guard_state
        self.enterContext(patch.object(policy, 'guard_state', return_value='absent'))
        self.context = safe_context()
        self.enterContext(patch.object(policy, 'current_session', return_value={k: self.context[k] for k in ('session', 'role')}))
        self.names = self.enterContext(patch.object(policy, '_names', return_value={'org.gnome.Shell'}))
        self.calls = self.enterContext(patch.object(policy, '_call', side_effect=lambda bus, name, path, interface, method, parameters=None:
            os.getuid() if method == 'GetConnectionUnixUser' else os.getpid()))

    def test_exact_receipt_accepts_current_shell_but_not_stale_pid_boot_or_session(self):
        record = policy.write_launch_receipt('system-fallback', self.context)
        self.assertTrue(policy.confirmed_system_fallback(object()))
        for field, value in (('start_time', record['start_time'] + 1), ('boot_id', 'previous-boot'),
                             ('session', '/another'), ('mode', 'private'), ('uid', os.getuid() + 1)):
            (policy.RUNTIME / policy.RECEIPT).write_text(json.dumps({**record, field: value}))
            self.assertFalse(policy.confirmed_system_fallback(object()), field)

    def test_guard_unknown_remote_owner_and_bus_failure_never_disable_backend(self):
        policy.write_launch_receipt('system-fallback', self.context)
        for state in ('present', 'unknown'):
            with patch.object(policy, 'guard_state', return_value=state):
                self.assertFalse(policy.confirmed_system_fallback(object()))
        self.names.return_value.add('org.gnome.Mutter.NativeTakeover')
        self.assertFalse(policy.confirmed_system_fallback(object()))
        self.names.return_value = {'org.gnome.Shell'}
        self.calls.side_effect = RuntimeError('bus unavailable')
        self.assertFalse(policy.confirmed_system_fallback(object()))

    def test_environment_hint_and_untrusted_receipt_are_not_authority(self):
        with patch.dict(os.environ, UBUNTU_NATIVE_SESSION_MODE='system-fallback'):
            self.assertFalse(policy.confirmed_system_fallback(object()))
        policy.write_launch_receipt('system-fallback', self.context)
        (policy.RUNTIME / policy.RECEIPT).chmod(0o666)
        self.assertFalse(policy.confirmed_system_fallback(object()))
        (policy.RUNTIME / policy.RECEIPT).unlink()
        (policy.RUNTIME / policy.RECEIPT).symlink_to('/missing')
        self.assertFalse(policy.confirmed_system_fallback(object()))

    def test_invalid_guard_and_unavailable_bus_are_unknown_not_safe(self):
        policy.GUARD.symlink_to('/missing')
        self.assertEqual(self.guard_check(policy.GUARD), 'unknown')
        self.assertEqual(self.guard_check(self.root / 'absent-guard'), 'unknown')
        with patch.object(policy, '_connect', side_effect=OSError('not available')):
            result = policy.inspect_startup()
        self.assertFalse(result['known'])
        with self.assertRaises(policy.StartupRefused):
            policy.choose_mode({'status': 'incompatible'}, result)

    def test_launch_lock_excludes_simultaneous_shell_and_is_inherited(self):
        first = policy.acquire_launch_lock()
        try:
            self.assertTrue(os.get_inheritable(first))
            with self.assertRaises(BlockingIOError):
                policy.acquire_launch_lock()
        finally:
            os.close(first)


class BackendCompatibilityTests(unittest.TestCase):
    def test_confirmed_fallback_exits_backend_without_spawning_or_connecting(self):
        supervisor = module('fallback_supervisor', ROOT / 'scripts/native-session.py')
        with patch.object(supervisor, 'confirmed_system_fallback', return_value=True), \
             patch.object(supervisor, 'Watchdog'), patch.object(supervisor.subprocess, 'Popen') as spawn, \
             patch.object(supervisor.Gio, 'bus_get_sync') as connect, contextlib.redirect_stdout(io.StringIO()):
            supervisor.main()
        spawn.assert_not_called()
        connect.assert_not_called()

    def test_finish_guard_skips_only_confirmed_greeter_or_receipt(self):
        guard = module('finish_login_guard', ROOT / 'scripts/finish-login-guard.py')
        with patch.object(guard, 'find_native_greeter_session', return_value='/real-greeter'), \
             patch.object(guard.Gio, 'bus_get_sync') as connect:
            guard.main()
            connect.assert_not_called()
        with patch.object(guard, 'find_native_greeter_session', return_value=None), \
             patch.object(guard, 'confirmed_system_fallback', return_value=True), \
             patch.object(guard.Gio, 'bus_get_sync') as connect, contextlib.redirect_stdout(io.StringIO()):
            guard.main()
            connect.assert_not_called()

    def test_unexpected_daemon_exit_is_failure_even_if_child_exits_zero(self):
        supervisor = module('native_session', ROOT / 'scripts/native-session.py')
        supervisor.report_child_exit(Mock(poll=lambda: None), Mock(poll=lambda: None))
        for code in (0, 1, -11):
            with self.assertRaisesRegex(RuntimeError, f'RDP daemon exited with status {code}'):
                supervisor.report_child_exit(Mock(poll=lambda: None), Mock(poll=lambda: code))

    def test_guarded_startup_defers_journal_recovery_to_the_broker(self):
        supervisor = module('native_session', ROOT / 'scripts/native-session.py')
        with patch.object(supervisor.subprocess, 'run') as recover:
            supervisor.recover_monitor_layout_before_broker('ready-guarded')
            recover.assert_not_called()
            supervisor.recover_monitor_layout_before_broker('local')
            recover.assert_called_once_with(['/usr/bin/python3', '-m', 'takeover.monitor_layout'], cwd=ROOT, check=True)
        with patch.object(supervisor.subprocess, 'run') as recover:
            with self.assertRaisesRegex(RuntimeError, 'requires a ready'):
                supervisor.recover_monitor_layout_before_broker('startup-failed')
            recover.assert_not_called()


if __name__ == '__main__':
    unittest.main()
