"""Activation/rollback tests with a temporary home and no live service changes."""
import argparse
import contextlib
import importlib.util
import io
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_SHELL_UNIT = Path('/usr/lib/systemd/user/org.gnome.Shell@.service')
SHELL_UNIT_FIXTURE = ('[Unit]\nDescription=GNOME Shell fixture\n'
                      '[Service]\nExecStart=/usr/bin/gnome-shell --mode=%i\n')


class PackageActivation(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.environment = patch.dict(os.environ, HOME=str(self.home))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        mode = os.umask(0o077)
        self.addCleanup(os.umask, mode)
        spec = importlib.util.spec_from_file_location('native_package_control', ROOT / 'packaging/control.py')
        self.control = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.control)
        self.control.ROOT = Path('/opt/ubuntu-native-takeover')
        self.control.LOGIN_CONFIG = self.home / 'login-config.json'
        self.args = argparse.Namespace(username='test-rdp', dry_run=False)
        # Exercise the real unit parser and generated overrides without
        # requiring GNOME to be installed on the test host. Other reads still
        # access the temporary files whose preservation these tests verify.
        read_text = Path.read_text

        def fixture_read(path, *args, **kwargs):
            if path == SYSTEM_SHELL_UNIT:
                return SHELL_UNIT_FIXTURE
            return read_text(path, *args, **kwargs)

        shell_unit = patch.object(Path, 'read_text', fixture_read)
        shell_unit.start()
        self.addCleanup(shell_unit.stop)

    def command(self, args, **kwargs):
        if args[0] == 'openssl':
            Path(args[args.index('-keyout') + 1]).write_text('test key placeholder')
            Path(args[args.index('-out') + 1]).write_text('test certificate placeholder')
        return subprocess.CompletedProcess(args, 0, '', '')

    def test_incompatible_setup_changes_nothing(self):
        with patch.object(self.control, 'check', return_value={'compatible': False}), patch.object(self.control.subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):
                self.control.setup(self.args)
        self.assertFalse(run.called)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_package_setup_requires_verified_candidate_and_intact_files(self):
        self.control.ROOT = self.home
        (self.home / 'build').mkdir()
        system = self.home / 'system-library'
        system.write_bytes(b'updated')
        (self.home / 'build/native-system-abi.json').write_text(json.dumps({str(system): 'old-hash'}))
        package = self.home / 'payload'
        package.write_bytes(b'intact')
        (self.home / 'package-files.json').write_text(json.dumps({
            'payload': hashlib.sha256(package.read_bytes()).hexdigest()}))
        candidate = {'status': 'verified', 'production_eligible': True}
        with patch.object(self.control, 'inspect_candidate', return_value=candidate):
            report = self.control.check()
            self.assertTrue(report['compatible'])
            candidate['status'] = 'needs-validation'
            self.assertFalse(self.control.check()['compatible'])
            candidate['status'] = 'verified'
            candidate['production_eligible'] = False
            self.assertFalse(self.control.check()['compatible'])
            candidate['production_eligible'] = True
            package.write_bytes(b'broken')
            self.assertFalse(self.control.check()['compatible'])

    def test_existing_configuration_is_preserved(self):
        files = self.control.launch_files()
        target = next(iter(files))
        target.parent.mkdir(parents=True)
        target.write_text('existing config')
        with patch.object(self.control, 'check', return_value={'compatible': True}), patch.object(self.control.subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):
                self.control.setup(self.args)
        self.assertEqual(target.read_text(), 'existing config')
        self.assertFalse(run.called)

    def test_dry_run_does_not_prompt_or_write(self):
        self.args.dry_run = True
        with patch.object(self.control, 'check', return_value={'compatible': True}), patch.object(self.control.getpass, 'getpass') as prompt, contextlib.redirect_stdout(io.StringIO()):
            self.control.setup(self.args)
        self.assertFalse(prompt.called)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_setup_and_rollback_preserve_work(self):
        work = self.home / 'unsaved-work-marker.txt'
        work.write_text('preserve')
        with patch.object(self.control, 'check', return_value={'compatible': True}), \
             patch.object(self.control.getpass, 'getpass', return_value='test-only-secret'), \
             patch.object(self.control.subprocess, 'run', side_effect=self.command) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            self.control.setup(self.args)
        calls = run.call_args_list
        credentials = [c for c in calls if 'set-credentials' in c.args[0]]
        self.assertEqual(len(credentials), 1)
        self.assertNotIn('test-only-secret', credentials[0].args[0])
        self.assertEqual(credentials[0].kwargs['input'], 'test-only-secret\n')
        self.assertFalse(any('restart' in c.args[0] or 'start' in c.args[0] for c in calls))
        for path in self.control.launch_files():
            self.assertTrue(path.exists())
        shell_override = self.control.UNITS / 'org.gnome.Shell@ubuntu.service.d/90-native-takeover.conf'
        self.assertIn(' -- /usr/bin/gnome-shell --mode=%i\n', shell_override.read_text())
        with patch('subprocess.run', side_effect=self.command), contextlib.redirect_stdout(io.StringIO()):
            runpy.run_path(str(ROOT / 'scripts/disable-native.py'), run_name='__main__')
        self.assertTrue(all(not p.exists() for p in self.control.launch_files()))
        self.assertEqual(work.read_text(), 'preserve')
        self.assertTrue((self.control.DATA / 'tls.key').exists())

    def test_remove_guard_preserves_system_login_entrypoint(self):
        self.control.LOGIN_CONFIG.write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'disable-login.py'):
            self.control.check_removal()

    def test_remove_guard_blocks_enabled_user(self):
        target = next(iter(self.control.launch_files()))
        target.parent.mkdir(parents=True)
        target.write_text('ExecStart=/opt/ubuntu-native-takeover/control.py')
        account = types.SimpleNamespace(pw_name='test-user', pw_dir=str(self.home))
        with patch.object(self.control.pwd, 'getpwall', return_value=[account]):
            with self.assertRaisesRegex(RuntimeError, 'test-user'):
                self.control.check_removal()


if __name__ == '__main__':
    unittest.main()
