import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('startup_repair', ROOT / 'scripts/install-greeter-startup-fix.py')
REPAIR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPAIR)
BASE = REPAIR.BASE
EXECUTABLE = 'build/grd-prefix/libexec/gnome-remote-desktop-daemon'
REMOVED = 'build/grd-prefix/share/obsolete.ptx'
ADDED = 'build/grd-prefix/share/gnome-remote-desktop/new.ptx'
LINK = 'build/grd-prefix/share/gnome-remote-desktop/current.ptx'


def manifest(directory):
    files, links = {}, {}
    for path in directory.rglob('*'):
        relative = path.relative_to(directory).as_posix()
        if path.is_symlink():
            links[relative] = os.readlink(path)
        elif path.is_file():
            files[relative] = BASE.digest(path)
    return {'version': 1, 'files': files, 'symlinks': links}


class StartupRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'root'
        self.package = Path(temporary.name) / 'package'
        self.package.mkdir()
        self.calls = []
        self.account = SimpleNamespace(pw_name='desktop', pw_uid=max(1000, os.getuid()),
                                       pw_gid=os.getgid(), pw_dir='/home/desktop')
        self.put('/etc/ubuntu-native-login/config.json', '{"user":"desktop"}')
        self.put(BASE.GATEWAY, '[Service]\nWorkingDirectory=' + BASE.PREFIX + '\n')
        self.put('/usr/lib/test-abi.so', 'unchanged system ABI')
        self.prefix = self.root / BASE.PREFIX.lstrip('/')
        self.put(BASE.PREFIX + '/' + REPAIR.HEALTH, 'old readiness implementation\n')
        self.put(BASE.PREFIX + '/takeover/login_gateway.py', 'unchanged gateway\n')
        self.put(BASE.PREFIX + '/build/native-prefix/lib/libmutter.so', 'unchanged Mutter\n')
        self.put(BASE.PREFIX + '/' + REMOVED, 'obsolete resource\n')
        self.put(BASE.PREFIX + '/' + EXECUTABLE, Path('/usr/bin/dash').read_bytes(), mode=0o755)
        for directory in [self.prefix, *(path for path in self.prefix.rglob('*') if path.is_dir())]:
            directory.chmod(0o755)
        self.old_manifest = manifest(self.prefix)
        old_bytes = (json.dumps(self.old_manifest, indent=2) + '\n').encode()
        (self.package / REPAIR.ANCHOR).write_bytes(old_bytes)
        self.old_hash = BASE.digest(self.package / REPAIR.ANCHOR)
        shutil.copytree(self.prefix, self.package / 'payload', symlinks=True)
        payload = self.package / 'payload'
        (payload / REPAIR.HEALTH).write_text('fixed readiness implementation\n')
        (payload / EXECUTABLE).write_bytes(Path('/usr/bin/python3').read_bytes())
        (payload / REMOVED).unlink()
        (payload / ADDED).parent.mkdir(parents=True, exist_ok=True)
        (payload / ADDED).write_text('new PTX resource\n')
        (payload / LINK).symlink_to('new.ptx')
        self.write_manifest()
        (self.package / 'system-abi.json').write_text(json.dumps({
            '/usr/lib/test-abi.so': BASE.digest(self.root / 'usr/lib/test-abi.so')}))
        self.base = BASE.Installer(self.package, root=self.root, runner=self.command,
            account_lookup=lambda name: self.account, owner_uid=os.getuid(),
            owner_gid=os.getgid(), gdm_gid=os.getgid())
        self.state = {'version': BASE.VERSION, 'phase': 'active', 'gateway_active': True,
                      'bundle_sha256': self.old_hash, 'payload_manifest': self.old_manifest,
                      'installed': {BASE.GATEWAY: BASE.metadata(self.base.path(BASE.GATEWAY))},
                      'original': {'existing-backups': 'must remain unchanged'},
                      'desired_gateway': 'must remain unchanged', 'user': 'desktop',
                      'activation_before': {'session': '/physical', 'pid': 11}}
        self.base.save(self.state)
        self.old_state_bytes = self.base.state_file.read_bytes()
        self.repair = REPAIR.StartupRepair(self.base)
        for pid in (101, 201, 202, 301):
            self.put(f'/proc/{pid}/status', f'State:\tS (sleeping)\nCoreDumping:\t0\nUid:\t{os.getuid()}\n')
            self.put(f'/proc/{pid}/stat', f'{pid} (process) ' + ' '.join(['S'] + ['0'] * 18 + [str(pid * 100)]) + '\n')
        self.put('/proc/201/task/201/children', '202\n')
        self.put('/proc/202/cmdline', (BASE.PREFIX + '/' + EXECUTABLE + '\0--headless\0').encode())

    def put(self, absolute, data, mode=0o644):
        path = self.root / absolute.lstrip('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
        path.chmod(mode)
        return path

    def command(self, args, **kwargs):
        self.calls.append(args)
        self.assertFalse(set(args) & {'start', 'stop', 'restart', 'reload', 'daemon-reload', 'kill'})
        names = {'org.gnome.Shell@ubuntu.service': 101,
                 'ubuntu-native-takeover.service': 201,
                 'ubuntu-native-login.service': 301}
        matches = [name for name in names if name in args]
        self.assertEqual(len(matches), 1, args)
        self.assertIn('show', args)
        return subprocess.CompletedProcess(args, 0, f'MainPID={names[matches[0]]}\nActiveState=active\n', '')

    def write_manifest(self):
        (self.package / 'payload-manifest.json').write_text(json.dumps(manifest(self.package / 'payload'), indent=2) + '\n')

    def assert_old_files(self):
        self.base.verify_prefix(self.old_manifest)
        self.assertFalse((self.prefix / ADDED).exists())
        self.assertFalse((self.prefix / LINK).is_symlink())

    def test_apply_only_changes_allowed_payload_and_preserves_installation_state(self):
        result = self.repair.apply()
        self.assertFalse(result['services_changed'])
        self.assertEqual(result['running_processes_unchanged'], self.repair.runtime_snapshot())
        self.assertFalse((self.prefix / REMOVED).exists())
        self.assertEqual((self.prefix / ADDED).read_text(), 'new PTX resource\n')
        self.assertEqual(os.readlink(self.prefix / LINK), 'new.ptx')
        self.assertEqual((self.prefix / EXECUTABLE).stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.prefix / ADDED).stat().st_mode & 0o777, 0o644)
        state = self.base.load()
        for key in set(self.state) - {'bundle_sha256', 'payload_manifest'}:
            self.assertEqual(state[key], self.state[key])
        self.assertEqual(state['bundle_sha256'], BASE.digest(self.package / 'payload-manifest.json'))
        self.base.assert_current(state)
        self.assertTrue(all('show' in command for command in self.calls))

    def test_apply_and_rollback_are_idempotent(self):
        self.repair.apply()
        self.calls.clear()
        self.assertTrue(self.repair.apply()['idempotent'])
        self.assertEqual(self.calls, [])
        result = self.repair.rollback()
        self.assertFalse(result['services_changed'])
        self.assert_old_files()
        self.assertEqual(self.base.load(), self.state)
        self.calls.clear()
        self.assertTrue(self.repair.rollback()['idempotent'])
        self.assertEqual(self.calls, [])

    def test_running_executable_is_replaced_atomically_without_stopping_its_process(self):
        executable = self.prefix / EXECUTABLE
        child = subprocess.Popen([str(executable), '-c', 'read token'], stdin=subprocess.PIPE)
        def cleanup():
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=2)
        self.addCleanup(cleanup)
        original_inode = executable.stat().st_ino
        with executable.open('rb') as old_descriptor:
            original_bytes = old_descriptor.read()
            self.repair.apply()
            self.assertIsNone(child.poll())
            self.assertNotEqual(executable.stat().st_ino, original_inode)
            old_descriptor.seek(0)
            self.assertEqual(old_descriptor.read(), original_bytes)
            self.assertTrue(os.readlink(f'/proc/{child.pid}/exe').endswith(' (deleted)'))
            self.repair.rollback()
            self.assertIsNone(child.poll())
        self.assertEqual(executable.read_bytes(), original_bytes)

    def test_old_manifest_anchor_requires_exact_bytes_and_root_manifest_match(self):
        anchor = self.package / REPAIR.ANCHOR
        anchor.write_bytes(anchor.read_bytes() + b'\n')
        with self.assertRaisesRegex(RuntimeError, 'anchor does not exactly match'):
            self.repair.apply()
        self.assert_old_files()
        self.assertFalse(self.repair.record_path.exists())

    def test_protected_mutter_or_gateway_change_is_rejected(self):
        for relative in ('takeover/login_gateway.py', 'build/native-prefix/lib/libmutter.so'):
            with self.subTest(relative=relative):
                target = self.package / 'payload' / relative
                original = target.read_bytes()
                target.write_text('unauthorized change\n')
                self.write_manifest()
                with self.assertRaisesRegex(RuntimeError, 'outside session_health.py'):
                    self.repair.apply()
                target.write_bytes(original)
                self.write_manifest()
                self.assert_old_files()
        self.assertEqual(self.calls, [])

    def test_changed_grd_symlink_cannot_escape_into_mutter(self):
        target = self.package / 'payload' / LINK
        target.unlink()
        target.symlink_to('../../../native-prefix/lib/libmutter.so')
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeError, 'escapes its resource tree'):
            self.repair.apply()
        self.assert_old_files()

    def test_failure_during_update_restores_added_removed_and_replaced_files_and_state(self):
        original = self.repair.replace_entry
        failed = False
        def fail_once(relative, source, expected):
            nonlocal failed
            if relative == EXECUTABLE and not failed:
                failed = True
                raise OSError('synthetic atomic publish failure')
            return original(relative, source, expected)
        with patch.object(self.repair, 'replace_entry', side_effect=fail_once):
            with self.assertRaisesRegex(RuntimeError, 'original files and state were restored'):
                self.repair.apply()
        self.assert_old_files()
        self.assertEqual(self.base.state_file.read_bytes(), self.old_state_bytes)
        self.assertEqual(self.repair.load_record()['status'], 'apply-failed-restored')
        self.assertTrue(self.repair.rollback()['idempotent'])

    def test_runtime_change_after_publish_restores_transaction_without_service_actions(self):
        before = self.repair.runtime_snapshot()
        changed = copy.deepcopy(before)
        changed['grd']['pid'] += 1
        with patch.object(self.repair, 'runtime_snapshot', side_effect=[before, changed]):
            with self.assertRaisesRegex(RuntimeError, 'original files and state were restored'):
                self.repair.apply()
        self.assert_old_files()
        self.assertEqual(self.base.state_file.read_bytes(), self.old_state_bytes)

    def test_rollback_refuses_later_administrator_file_edit(self):
        self.repair.apply()
        target = self.prefix / REPAIR.HEALTH
        target.write_text('later administrative change\n')
        with self.assertRaisesRegex(RuntimeError, 'subsequently modified'):
            self.repair.rollback()
        self.assertEqual(target.read_text(), 'later administrative change\n')
        self.assertEqual(self.repair.load_record()['status'], 'applied')

    def test_rollback_refuses_corrupted_backup_before_any_replacement(self):
        self.repair.apply()
        record = self.repair.load_record()
        source = self.repair.directory / record['before'][EXECUTABLE]['backup']
        source.write_text('corrupted backup')
        current = (self.prefix / REPAIR.HEALTH).read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'backup checksum'):
            self.repair.rollback()
        self.assertEqual((self.prefix / REPAIR.HEALTH).read_bytes(), current)


if __name__ == '__main__':
    unittest.main()
