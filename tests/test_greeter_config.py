import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from takeover import greeter_config


class GreeterConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_missing_and_mutable_credentials_fail_without_creation(self):
        missing = self.root / 'credentials.ini'
        with self.assertRaisesRegex(RuntimeError, 'Missing native greeter asset'):
            greeter_config._check(missing, secret=True, owner_uid=os.getuid(), group_gid=os.getgid())
        self.assertFalse(missing.exists())
        missing.write_text('dummy—not a real credential')
        missing.chmod(0o666)
        with self.assertRaisesRegex(RuntimeError, 'not group/world-writable'):
            greeter_config._check(missing, secret=True, owner_uid=os.getuid(), group_gid=os.getgid())
        missing.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'restricted to root'):
            greeter_config._check(missing, secret=True, owner_uid=os.getuid(), group_gid=os.getgid())
        missing.chmod(0o640)
        greeter_config._check(missing, secret=True, owner_uid=os.getuid(), group_gid=os.getgid())
        link = self.root / 'link'
        link.symlink_to(missing)
        with self.assertRaisesRegex(RuntimeError, 'not a regular'):
            greeter_config._check(link, owner_uid=os.getuid())

    def test_child_environment_preserves_dynamic_session_paths(self):
        config = self.root / 'config'
        config.mkdir()
        data = self.root / 'data'
        (data / 'rdp/gnome-remote-desktop').mkdir(parents=True)
        (config / 'dconf-profile').write_text(greeter_config.dconf_profile(config))
        original = {'HOME': '/run/gdm3/home/gdm-greeter',
                    'XDG_CONFIG_HOME': '/var/lib/gdm3/seat0/config',
                    'XDG_RUNTIME_DIR': '/run/user/60580',
                    'DCONF_PROFILE': 'gdm', 'XDG_DATA_HOME': '/dynamic/data'}
        # Individual permission checks above exercise unprivileged fixtures;
        # this test verifies the public environment-copy and asset-list logic.
        with patch.object(greeter_config, '_check') as check:
            child = greeter_config.grd_environment(original, config=config, data=data)
        self.assertEqual(original['DCONF_PROFILE'], 'gdm')
        self.assertEqual(child['DCONF_PROFILE'], str(config / 'dconf-profile'))
        self.assertEqual(child['XDG_DATA_HOME'], str(data / 'rdp'))
        for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_RUNTIME_DIR'):
            self.assertEqual(child[key], original[key])
        checked = {call.args[0] for call in check.call_args_list}
        self.assertIn(data / 'rdp/gnome-remote-desktop/credentials.ini', checked)
        self.assertIn(data / 'tls.key', checked)
        self.assertIn(config / 'settings.db', checked)
        self.assertIn(Path('/'), checked)

    @unittest.skipUnless(shutil.which('dconf') and shutil.which('gsettings'), 'dconf tools required')
    def test_compiled_file_database_is_read_only_and_does_not_create_user_database(self):
        source = self.root / 'settings.d'
        (source / 'locks').mkdir(parents=True)
        (source / '00-rdp').write_text(greeter_config.dconf_settings())
        (source / 'locks/00-rdp').write_text(greeter_config.DCONF_LOCKS)
        subprocess.run(['dconf', 'compile', str(self.root / 'settings.db'), str(source)], check=True)
        profile = self.root / 'dconf-profile'
        profile.write_text(greeter_config.dconf_profile(self.root))
        environment = dict(os.environ, DCONF_PROFILE=str(profile),
                           XDG_CONFIG_HOME=str(self.root / 'user-config'), GSETTINGS_BACKEND='dconf')
        environment.pop('GSETTINGS_SCHEMA_DIR', None)
        for operation, key, expected in [('get', 'enable', 'true'),
                                          ('get', 'port', 'uint16 3394'),
                                          ('get', 'negotiate-port', 'false'),
                                          ('writable', 'enable', 'false')]:
            result = subprocess.run(['gsettings', operation,
                                     'org.gnome.desktop.remote-desktop.rdp.headless', key],
                                    env=environment, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), expected)
            self.assertEqual(result.stderr, '')
        self.assertFalse((self.root / 'user-config').exists())


if __name__ == '__main__':
    unittest.main()
