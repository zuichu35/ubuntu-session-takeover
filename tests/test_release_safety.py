"""Publication entrypoints must not change live services without an opt-in."""
import contextlib
import importlib.util
import io
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


class ReleaseSafetyTests(unittest.TestCase):
    def test_legacy_retirement_requires_explicit_opt_in(self):
        supervisor = module('release_native_session', 'scripts/native-session.py')
        with patch.object(supervisor.subprocess, 'run') as run:
            supervisor.retire_legacy_services()
            run.assert_not_called()
            supervisor.retire_legacy_services(enabled=True)
            run.assert_called_once_with(
                ['systemctl', '--user', 'stop', 'ubuntu-takeover-broker.service',
                 'ubuntu-takeover-rdp.service'], check=False)

    def test_live_power_test_refuses_before_any_desktop_connection(self):
        with patch('os.getuid', return_value=42420), \
             patch('gi.repository.Gio.bus_get_sync') as bus, \
             patch('socket.socket') as socket:
            probe = module('release_live_power', 'tests/live_power_save.py')
            self.assertEqual(probe.gate, '/run/user/42420/ubuntu-native-takeover/gate.sock')
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
                probe.main([])
            self.assertEqual(failure.exception.code, 2)
            bus.assert_not_called()
            socket.assert_not_called()

    def test_historical_login_installer_fails_before_system_changes(self):
        installer = module('release_historical_login', 'scripts/install-login.py')
        with patch.object(installer.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'docs/DEPLOYMENT.md'):
                installer.main()
            run.assert_not_called()

    def test_legacy_cli_is_disabled_before_starting_services(self):
        with patch('subprocess.run') as run:
            with self.assertRaisesRegex(SystemExit, 'legacy 3391 CLI'):
                runpy.run_module('takeover.cli', run_name='__main__')
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
