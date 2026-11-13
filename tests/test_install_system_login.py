import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('install_system_login', ROOT / 'scripts/install-system-login.py')
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)


class SystemLoginInstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.calls = []
        prefix = self.root / INSTALL.PREFIX.lstrip('/')
        (prefix / 'takeover').mkdir(parents=True)
        for name in ('startup_guard.py', 'greeter_config.py'):
            shutil.copy(ROOT / 'takeover' / name, prefix / 'takeover' / name)
        self.account = SimpleNamespace(pw_name='desktop', pw_uid=max(1000, os.getuid()),
                                       pw_gid=os.getgid(), pw_dir='/home/desktop')
        self.put('/home/desktop/.config/systemd/user/org.gnome.Shell@ubuntu.service.d/90-native-takeover.conf',
                 'ExecStart=/usr/bin/python3 /opt/ubuntu-native-takeover/scripts/native-shell.py -- x\n')
        for relative in INSTALL.SECRETS:
            self.put('/home/desktop/.local/share/ubuntu-native-takeover/' + relative, 'synthetic\n')
        for directory in ('/etc/systemd/system', '/etc/systemd/user', '/var/lib'):
            (self.root / directory.lstrip('/')).mkdir(parents=True, exist_ok=True)

    def put(self, absolute, text):
        path = self.root / absolute.lstrip('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def runner(self, args, **kwargs):
        self.calls.append(args)
        if args[:2] == ['dconf', 'compile']:
            Path(args[2]).write_bytes(b'GVariant database')
        return subprocess.CompletedProcess(args, 0, '', '')

    def installer(self):
        return INSTALL.Installer('desktop', root=self.root, runner=self.runner,
                                 account_lookup=lambda name: self.account, gdm_gid=os.getgid(),
                                 owner_uid=os.getuid(), owner_gid=os.getgid())

    def test_install_writes_final_state_and_rollback_removes_it(self):
        result = self.installer().install()
        self.assertEqual(result['phase'], 'installed')
        gateway = (self.root / INSTALL.GATEWAY.lstrip('/')).read_text()
        self.assertIn('WorkingDirectory=/opt/ubuntu-native-takeover\n', gateway)
        self.assertIn('TimeoutStopSec=10s', gateway)
        shell = (self.root / INSTALL.SHELL_DROPIN.lstrip('/')).read_text()
        self.assertIn('/opt/ubuntu-native-takeover/scripts/native-shell.py -- /usr/bin/gnome-shell --mode=%i', shell)
        self.assertIn('ExecStartPost=-/usr/bin/systemctl --user --no-block start ubuntu-native-login-guard.service', shell)
        guard = (self.root / INSTALL.GUARD_UNIT.lstrip('/')).read_text()
        self.assertIn('/opt/ubuntu-native-takeover/scripts/finish-login-guard.py', guard)
        self.assertEqual(json.loads((self.root / INSTALL.LOGIN_CONFIG.lstrip('/')).read_text()), {'user': 'desktop'})
        profile = (self.root / INSTALL.GREETER_CONFIG.lstrip('/') / 'dconf-profile').read_text()
        self.assertEqual(profile, 'file-db:/etc/ubuntu-native-login/greeter/settings.db\n')
        key = self.root / INSTALL.GREETER_DATA.lstrip('/') / 'tls.key'
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(key.parent.stat().st_mode), 0o750)
        self.assertIn(['systemctl', 'enable', '--now', 'ubuntu-native-login.service'], self.calls)
        manifest = json.loads((self.root / INSTALL.MANIFEST.lstrip('/')).read_text())
        self.assertEqual(manifest['phase'], 'installed')
        self.assertTrue(all(previous is None for previous in manifest['files'].values()))
        self.assertEqual(set(manifest['files']), set(manifest['installed_hashes']))

        self.assertEqual(self.installer().rollback()['phase'], 'rolled-back')
        for absolute in (INSTALL.GATEWAY, INSTALL.SHELL_DROPIN, INSTALL.LOGIN_CONFIG, INSTALL.GREETER_DATA,
                         '/etc/ubuntu-native-login', '/etc/systemd/user/org.gnome.Shell@.service.d'):
            self.assertFalse((self.root / absolute.lstrip('/')).exists(), absolute)
        self.assertTrue((self.root / 'etc/systemd/user').is_dir())
        self.assertIn(['systemctl', 'disable', '--now', 'ubuntu-native-login.service'], self.calls)

    def test_refuses_existing_configuration_without_writing(self):
        self.put(INSTALL.GREETER_UNIT, 'foreign\n')
        with self.assertRaisesRegex(RuntimeError, 'Refusing to replace'):
            self.installer().install()
        self.assertFalse((self.root / INSTALL.GATEWAY.lstrip('/')).exists())
        self.assertEqual((self.root / INSTALL.GREETER_UNIT.lstrip('/')).read_text(), 'foreign\n')

    def test_refuses_without_per_user_setup_or_with_autologin(self):
        (self.root / 'home/desktop/.config/systemd/user/org.gnome.Shell@ubuntu.service.d/90-native-takeover.conf').unlink()
        self.put(INSTALL.GDM_CONFIG, '[daemon]\nAutomaticLoginEnable=True\n')
        problems = self.installer().preflight()
        self.assertEqual(len(problems), 2)
        with self.assertRaisesRegex(RuntimeError, 'Preflight failed'):
            self.installer().install()

    def test_rollback_refuses_modified_files_unless_forced(self):
        self.installer().install()
        self.put(INSTALL.GATEWAY, 'edited\n')
        with self.assertRaisesRegex(RuntimeError, 'changed since install'):
            self.installer().rollback()
        self.assertEqual(self.installer().rollback(force=True)['phase'], 'rolled-back')


if __name__ == '__main__':
    unittest.main()
