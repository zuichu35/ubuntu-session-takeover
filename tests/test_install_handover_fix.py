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
SPEC = importlib.util.spec_from_file_location('handover_install', ROOT / 'scripts/install-handover-fix.py')
FIX = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIX)
BASE = FIX.BASE


def manifest(tree):
    return {'version': 1, 'files': {str(p.relative_to(tree)): BASE.digest(p)
        for p in tree.rglob('*') if p.is_file()}, 'symlinks': {}}


class AtomicNextBootRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'system'
        self.package = Path(self.temp.name) / 'package'
        self.package.mkdir()
        self.account = SimpleNamespace(pw_name='desktop', pw_uid=1000, pw_gid=1000, pw_dir='/home/desktop')
        self.calls = []
        self.put('/etc/ubuntu-native-login/config.json', '{"user":"desktop"}')
        self.put('/usr/lib/abi', 'unchanged ABI')
        self.put(FIX.SHELL_UNIT, '[Service]\nExecStartPost=old-helper\n')
        self.put(BASE.PREFIX + '/scripts/native-session.py', 'old supervisor')
        self.put(BASE.PREFIX + '/build/native-prefix/lib/libmutter.so', 'old library')
        self.put(BASE.PREFIX + '/build/native-prefix/libexec/native-local-auth', 'unchanged PAM')
        self.put(BASE.PREFIX + '/build/grd-prefix/bin/daemon', Path('/usr/bin/dash').read_bytes(), 0o755)
        self.prefix = self.root / BASE.PREFIX.lstrip('/')
        for directory in [self.prefix, *(p for p in self.prefix.rglob('*') if p.is_dir())]:
            directory.chmod(0o755)
        self.old = manifest(self.prefix)
        (self.package / 'before-handover-manifest.json').write_text(json.dumps(self.old, indent=2) + '\n')
        shutil.copytree(self.prefix, self.package / 'payload')
        (self.package / 'payload/scripts/native-session.py').write_text('new supervisor')
        (self.package / 'payload/build/native-prefix/lib/libmutter.so').write_text('new library')
        (self.package / 'system-abi.json').write_text(json.dumps({'/usr/lib/abi': BASE.digest(self.root / 'usr/lib/abi')}))
        units = {}
        for index, absolute in enumerate(sorted(FIX.ALLOWED_UNITS)):
            source = self.package / f'unit-{index}.conf'
            source.write_text('[Service]\nExecStart=/usr/bin/true\n')
            units[absolute] = {'source': source.name, 'sha256': BASE.digest(source)}
        (self.package / 'units.json').write_text(json.dumps(units))
        self.write_manifest()
        self.base = BASE.Installer(self.package, root=self.root, runner=self.command,
            account_lookup=lambda _: self.account, owner_uid=os.getuid(), owner_gid=os.getgid(), gdm_gid=os.getgid())
        self.state = {'version': BASE.VERSION, 'phase': 'active', 'gateway_active': True,
            'payload_manifest': self.old, 'bundle_sha256': BASE.digest(self.package / 'before-handover-manifest.json'),
            'original': {FIX.SHELL_UNIT: {'exists': False}},
            'installed': {FIX.SHELL_UNIT: BASE.metadata(self.root / FIX.SHELL_UNIT.lstrip('/'))},
            'retained': [], 'desired_gateway': 'unchanged gateway', 'user': 'desktop'}
        self.base.save(self.state)
        self.fix = FIX.HandoverRepair(self.base)
        for pid in (101, 201, 202, 301):
            self.put(f'/proc/{pid}/stat', f'{pid} (worker) ' + ' '.join(['S'] + ['0'] * 18 + [str(pid * 10)]))
        self.put('/proc/201/task/201/children', '202')
        self.put('/proc/202/cmdline', b'/opt/test/gnome-remote-desktop-daemon\0--headless')

    def put(self, absolute, content, mode=0o644):
        p = self.root / absolute.lstrip('/')
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode() if isinstance(content, str) else content)
        p.chmod(mode)
        return p

    def command(self, args, **kwargs):
        self.calls.append(args)
        self.assertFalse(set(args) & {'start', 'stop', 'restart', 'reload', 'daemon-reload', 'kill'})
        names = {'org.gnome.Shell@ubuntu.service': 101,
                 'ubuntu-native-takeover.service': 201, 'ubuntu-native-login.service': 301}
        name = next(n for n in names if n in args)
        self.assertIn('show', args)
        return subprocess.CompletedProcess(args, 0, f'MainPID={names[name]}\nActiveState=active\n', '')

    def write_manifest(self):
        (self.package / 'payload-manifest.json').write_text(json.dumps(manifest(self.package / 'payload'), indent=2) + '\n')

    def test_atomic_publication_preserves_live_inode_cwd_and_process_then_rolls_back(self):
        child = subprocess.Popen([str(self.prefix / 'build/grd-prefix/bin/daemon'), '-c', 'read token'],
                                 cwd=self.prefix, stdin=subprocess.PIPE)
        def cleanup():
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=2)
        self.addCleanup(cleanup)
        original_inode = self.prefix.stat().st_ino
        old_descriptor = (self.prefix / 'scripts/native-session.py').open()
        self.addCleanup(old_descriptor.close)
        result = self.fix.apply()
        self.assertFalse(result['services_changed'])
        self.assertIsNone(child.poll())
        self.assertNotEqual(self.prefix.stat().st_ino, original_inode)
        self.assertEqual(Path(f'/proc/{child.pid}/cwd').stat().st_ino, original_inode)
        self.assertEqual(old_descriptor.read(), 'old supervisor')
        self.assertEqual((self.prefix / 'scripts/native-session.py').read_text(), 'new supervisor')
        self.assertTrue(self.fix.apply()['idempotent'])
        self.fix.rollback()
        self.assertIsNone(child.poll())
        self.assertEqual(self.prefix.stat().st_ino, original_inode)
        self.assertEqual(self.base.load(), self.state)
        self.assertFalse(self.base.path(FIX.GUARD_UNIT).exists())
        self.assertTrue(self.fix.rollback()['idempotent'])

    def test_unit_publication_failure_restores_complete_tree_and_configuration(self):
        original = self.base.write
        failed = False
        def fail_once(absolute, data, **kwargs):
            nonlocal failed
            if absolute == FIX.GUARD_UNIT and not failed:
                failed = True
                raise OSError('injected filesystem failure')
            return original(absolute, data, **kwargs)
        with patch.object(self.base, 'write', side_effect=fail_once):
            with self.assertRaisesRegex(RuntimeError, 'previous runtime/configuration restored'):
                self.fix.apply()
        self.base.assert_current(self.state)
        self.assertEqual(self.base.load(), self.state)
        self.assertFalse(self.base.path(FIX.GUARD_UNIT).exists())

    def test_runtime_change_aborts_and_restores(self):
        before = self.fix.snapshot()
        after = {**before, 'unrelated': {}}
        with patch.object(self.fix, 'snapshot', side_effect=[before, after]):
            with self.assertRaisesRegex(RuntimeError, 'protected process identity changed'):
                self.fix.apply()
        self.base.assert_current(self.state)

    def test_checksum_drift_and_scope_violation_rejected_before_mutation(self):
        for relative in ('build/grd-prefix/bin/daemon', 'build/native-prefix/libexec/native-local-auth'):
            with self.subTest(relative=relative):
                p = self.package / 'payload' / relative
                old = p.read_bytes()
                p.write_bytes(b'unreviewed change')
                self.write_manifest()
                with self.assertRaisesRegex(RuntimeError, 'Out-of-scope|PAM helper'):
                    self.fix.apply()
                p.write_bytes(old)
                self.write_manifest()
                self.base.assert_current(self.state)
        self.assertEqual(self.calls, [])

    def test_existing_unit_edit_is_not_overwritten(self):
        self.base.path(FIX.SHELL_UNIT).write_text('admin edit')
        with self.assertRaisesRegex(RuntimeError, 'configuration was changed'):
            self.fix.apply()
        self.assertEqual(self.base.path(FIX.SHELL_UNIT).read_text(), 'admin edit')

    def test_later_unit_edit_prevents_rollback(self):
        self.fix.apply()
        self.base.path(FIX.GUARD_UNIT).write_text('admin edit')
        with self.assertRaisesRegex(RuntimeError, 'Later configuration edit'):
            self.fix.rollback()
        self.assertEqual((self.prefix / 'scripts/native-session.py').read_text(), 'new supervisor')

    def test_corrupt_backup_prevents_rollback(self):
        self.fix.apply()
        record = json.loads(self.fix.record_path.read_text())
        previous = Path(record['exchange_directory'])
        (previous / 'scripts/native-session.py').write_text('bad backup')
        with self.assertRaisesRegex(RuntimeError, 'Runtime checksum changed'):
            self.fix.rollback()
        self.assertEqual((self.prefix / 'scripts/native-session.py').read_text(), 'new supervisor')

    def test_interrupted_exchange_recovers_using_directory_identity(self):
        self.fix.apply()
        record = json.loads(self.fix.record_path.read_text())
        record['status'] = 'prepared'
        self.fix.save(record)
        self.fix.rollback()
        self.base.assert_current(self.state)
        self.assertEqual(self.base.load(), self.state)

    def test_rollback_does_not_require_a_working_rdp_backend(self):
        self.fix.apply()
        self.base.path('/proc/201/task/201/children').write_text('')
        self.fix.rollback()
        self.base.assert_current(self.state)

    def test_publishes_only_unit_bytes_verified_before_staging(self):
        stage = self.fix.stage
        def mutate_source_after_validation(manifest):
            for item in json.loads((self.package / 'units.json').read_text()).values():
                (self.package / item['source']).write_text('unverified later source change')
            return stage(manifest)
        with patch.object(self.fix, 'stage', side_effect=mutate_source_after_validation):
            self.fix.apply()
        for absolute in FIX.ALLOWED_UNITS:
            self.assertEqual(self.base.path(absolute).read_text(), '[Service]\nExecStart=/usr/bin/true\n')
        self.fix.rollback()
        self.base.assert_current(self.state)


if __name__ == '__main__':
    unittest.main()
