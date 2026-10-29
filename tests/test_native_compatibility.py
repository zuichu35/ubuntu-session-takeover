import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.launcher = module('native_shell', ROOT / 'scripts/native-shell.py')
        self.launcher.MANIFEST = self.root / 'abi.json'

    def test_changed_system_file_is_reported_but_private_launch_is_preserved(self):
        library = self.root / 'libmutter.so'
        library.write_bytes(b'verified version')
        self.launcher.MANIFEST.write_text(json.dumps({
            str(library): hashlib.sha256(library.read_bytes()).hexdigest()}))
        self.assertEqual(self.launcher.mismatches(), [])
        library.write_bytes(b'distribution update')
        self.assertEqual(self.launcher.mismatches(), [str(library)])
        with patch.object(self.launcher.os, 'execvpe') as execute, \
             patch.dict(os.environ, MUTTER_NATIVE_TAKEOVER_TEST='1', MUTTER_NATIVE_TEST_AUTH_HELPER='/test-only'), \
             contextlib.redirect_stderr(io.StringIO()) as log:
            self.launcher.main(['--', '/original-wrapper', '/usr/bin/gnome-shell', '--mode=ubuntu'])
        command, argv, env = execute.call_args.args
        self.assertEqual(command, '/original-wrapper')
        self.assertEqual(argv, ['/original-wrapper', '/usr/bin/gnome-shell', '--mode=ubuntu'])
        self.assertEqual(env['MUTTER_NATIVE_TAKEOVER'], '1')
        self.assertTrue(env['LD_LIBRARY_PATH'].startswith(str(ROOT / 'build/native-prefix/lib')))
        self.assertNotIn('MUTTER_NATIVE_TAKEOVER_TEST', env)
        self.assertNotIn('MUTTER_NATIVE_TEST_AUTH_HELPER', env)
        self.assertIn('continuing with the private build', log.getvalue())

    def test_missing_corrupt_or_empty_manifest_does_not_disable_takeover(self):
        for contents in (None, '{bad', '{}', '[]', 'null'):
            if contents is not None:
                self.launcher.MANIFEST.write_text(contents)
            with patch.object(self.launcher.os, 'execvpe') as execute, contextlib.redirect_stderr(io.StringIO()):
                self.launcher.main(['/usr/bin/gnome-shell'])
            self.assertEqual(execute.call_args.args[2]['MUTTER_NATIVE_TAKEOVER'], '1')

    def test_check_is_diagnostic_not_a_startup_gate(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.launcher.main(['--check'])
        report = json.loads(out.getvalue())
        self.assertFalse(report['native_build_matches_system'])
        self.assertFalse(report['hash_mismatch_blocks_startup'])
        self.assertEqual(report['startup_policy'], 'attempt-load')

    def test_actual_exec_failure_reports_error(self):
        with patch.object(self.launcher.os, 'execvpe', side_effect=FileNotFoundError('executable missing')), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, 'failed to execute'):
                self.launcher.main(['/missing/gnome-shell'])

    def test_guarded_login_requires_private_mutter_in_loader_resolution(self):
        with patch('takeover.login_gateway.boot_guard_present', return_value=True), \
             patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, '/usr/lib/libmutter-51.so', '')), \
             patch.object(self.launcher.os, 'execvpe') as execute, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, 'Cannot guarantee startup privacy'):
                self.launcher.main(['/usr/bin/gnome-shell'])
            execute.assert_not_called()

    def test_guarded_login_passes_privacy_flag_before_exec(self):
        library = str(ROOT / 'build/native-prefix/lib/x86_64-linux-gnu/libmutter-18.so.0')
        with patch('takeover.login_gateway.boot_guard_present', return_value=True), \
             patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, library, '')), \
             patch.object(self.launcher.os, 'execvpe') as execute, \
             contextlib.redirect_stderr(io.StringIO()):
            self.launcher.main(['/usr/bin/gnome-shell'])
            self.assertEqual(execute.call_args.args[2]['MUTTER_NATIVE_BOOT_PRIVACY'], '1')

    def test_real_dynamic_greeter_gets_greeter_role_without_desktop_boot_guard(self):
        with patch('takeover.greeter_identity.find_native_greeter_session', return_value='/real-greeter'), \
             patch('takeover.login_gateway.boot_guard_present', return_value=True), \
             patch.object(self.launcher.os, 'execvpe') as execute, \
             contextlib.redirect_stderr(io.StringIO()):
            self.launcher.main(['/usr/bin/gnome-shell', '--mode=gdm'])
        env = execute.call_args.args[2]
        self.assertEqual(env['MUTTER_NATIVE_GREETER'], '1')
        self.assertNotIn('MUTTER_NATIVE_BOOT_PRIVACY', env)

    def test_gdm_mode_and_inherited_flag_do_not_grant_greeter_role(self):
        with patch('takeover.greeter_identity.find_native_greeter_session', return_value=None), \
             patch('takeover.login_gateway.boot_guard_present', return_value=False), \
             patch.dict(os.environ, MUTTER_NATIVE_GREETER='1'), \
             patch.object(self.launcher.os, 'execvpe') as execute, \
             contextlib.redirect_stderr(io.StringIO()):
            self.launcher.main(['/usr/bin/gnome-shell', '--mode=gdm'])
        self.assertNotIn('MUTTER_NATIVE_GREETER', execute.call_args.args[2])

    def test_physical_greeter_recovers_after_transient_logind_failure(self):
        with patch('takeover.greeter_identity.find_native_greeter_session',
                   side_effect=[None, '/confirmed-greeter']) as identify, \
             patch('takeover.greeter_identity.is_gdm_group_member', return_value=True), \
             patch.dict(os.environ, GDM_SEAT_ID='seat0'), \
             patch.object(self.launcher.time, 'sleep') as sleep, \
             patch.object(self.launcher.os, 'execvpe') as execute, \
             contextlib.redirect_stderr(io.StringIO()):
            self.launcher.main(['/usr/bin/gnome-shell', '--mode=gdm'])
        self.assertEqual(identify.call_count, 2)
        sleep.assert_called_once_with(.25)
        self.assertEqual(execute.call_args.args[2]['MUTTER_NATIVE_GREETER'], '1')

    def test_unconfirmed_physical_greeter_never_executes_as_ordinary_desktop(self):
        with patch('takeover.greeter_identity.find_native_greeter_session', return_value=None) as identify, \
             patch('takeover.greeter_identity.is_gdm_group_member', return_value=True), \
             patch.dict(os.environ, GDM_SEAT_ID='seat0', MUTTER_NATIVE_GREETER='1'), \
             patch.object(self.launcher.time, 'sleep') as sleep, \
             patch.object(self.launcher.os, 'execvpe') as execute, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, 'after 3 attempts'):
                self.launcher.main(['/usr/bin/gnome-shell', '--mode=gdm'])
        self.assertEqual(identify.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        execute.assert_not_called()

    def test_ordinary_user_and_seatless_greeter_do_not_wait(self):
        for mode, gdm_member, seat in [('ubuntu', False, 'seat0'), ('gdm', True, '')]:
            with self.subTest(mode=mode, seat=seat), \
                 patch('takeover.greeter_identity.find_native_greeter_session', return_value=None) as identify, \
                 patch('takeover.greeter_identity.is_gdm_group_member', return_value=gdm_member), \
                 patch('takeover.login_gateway.boot_guard_present', return_value=False), \
                 patch.dict(os.environ, GDM_SEAT_ID=seat, XDG_SEAT=seat), \
                 patch.object(self.launcher.time, 'sleep') as sleep, \
                 patch.object(self.launcher.os, 'execvpe') as execute, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.launcher.main(['/usr/bin/gnome-shell', f'--mode={mode}'])
            identify.assert_called_once_with()
            sleep.assert_not_called()
            execute.assert_called_once()
            self.assertNotIn('MUTTER_NATIVE_GREETER', execute.call_args.args[2])

    def test_finish_guard_skips_only_confirmed_real_greeter(self):
        guard = module('finish_login_guard', ROOT / 'scripts/finish-login-guard.py')
        with patch.object(guard, 'find_native_greeter_session', return_value='/real-greeter'), \
             patch.object(guard.Gio, 'bus_get_sync') as connect:
            guard.main()
            connect.assert_not_called()
        bus = Mock()
        bus.call_sync.return_value.unpack.return_value = ('boot-guard',)
        with patch.object(guard, 'find_native_greeter_session', return_value=None), \
             patch.object(guard.Gio, 'bus_get_sync', return_value=bus), \
             patch.object(guard.subprocess, 'check_output', return_value=b'[]'), \
             patch.dict(os.environ, MUTTER_NATIVE_GREETER='1'):
            with self.assertRaisesRegex(SystemExit, 'startup privacy remains active'):
                guard.main()

    def test_real_exec_survives_mismatch_and_preserves_runtime_failure_status(self):
        # Use a harmless child process: actually exec, but never start/stop a desktop.
        loader = ("import importlib.util, pathlib, sys; "
                  "s=importlib.util.spec_from_file_location('launcher',sys.argv[1]); "
                  "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                  "m.MANIFEST=pathlib.Path(sys.argv[2]); m.main(sys.argv[3:])")
        child = "import os,sys; print(os.environ['MUTTER_NATIVE_TAKEOVER']); sys.exit(7)"
        result = subprocess.run(['/usr/bin/python3', '-c', loader, str(ROOT / 'scripts/native-shell.py'),
                                 str(self.launcher.MANIFEST), '--', '/usr/bin/python3', '-c', child],
                                capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), '1')
        self.assertEqual(result.returncode, 7)
        self.assertIn('continuing with the private build', result.stderr)

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
            recover.assert_called_once_with(
                ['/usr/bin/python3', '-m', 'takeover.monitor_layout'], cwd=ROOT, check=True)
        with patch.object(supervisor.subprocess, 'run') as recover:
            with self.assertRaisesRegex(RuntimeError, 'requires a ready'):
                supervisor.recover_monitor_layout_before_broker('startup-failed')
            recover.assert_not_called()


if __name__ == '__main__':
    unittest.main()
