import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from takeover.remote_settings import (client_scale, load_multi_monitor, load_scale, save_multi_monitor,
                                      save_scale, settings_path, validate_scale)
from takeover.monitor_layout import MonitorLayout, remote_monitor, snapshot
from takeover.native_broker import Broker
from test_monitor_layout import FakeBus


class SettingsTests(unittest.TestCase):
    def test_default_persistence_and_validation(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, XDG_CONFIG_HOME=directory):
            self.assertEqual(load_scale(), 150)
            save_scale(175)
            self.assertEqual(load_scale(), 175)
            self.assertEqual(settings_path().stat().st_mode & 0o777, 0o600)
            for invalid in [True, 0, 99, 151, 201, float('nan'), '150']:
                with self.assertRaises(ValueError): validate_scale(invalid)
            self.assertEqual(load_scale(), 175)

    def test_multi_monitor_switch_is_independent_of_scale(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, XDG_CONFIG_HOME=directory):
            self.assertTrue(load_multi_monitor())
            save_scale(125)
            save_multi_monitor(False)
            save_scale(100)
            self.assertFalse(load_multi_monitor())
            self.assertEqual(load_scale(), 100)
            self.assertEqual(json.loads(settings_path().read_text()),
                             {'scale_percent': 100, 'multi_monitor': False})
            with self.assertRaises(ValueError):
                save_multi_monitor('off')
            settings_path().write_text('{"multi_monitor": "yes"}')
            with self.assertRaises(ValueError):
                load_multi_monitor()

    def test_live_scale_keeps_pixel_size_and_restores_local_layout(self):
        bus = FakeBus()
        bus.current[3].update({'native-takeover-viewport-supported': True,
                               'native-takeover-scale-supported': True})
        before = snapshot(bus.current)
        with tempfile.TemporaryDirectory() as directory:
            layout = MonitorLayout(bus, Path(directory) / 'layout.json', scale_percent=100)
            layout.enter()
            original = layout.journal.read_bytes()
            layout.resize(2560, 1440)
            for percent in (125, 150, 175, 200):
                layout.set_scale(percent)
                self.assertEqual(bus.current[2][0][2], percent / 100)
                self.assertEqual(tuple(bus.current[3]['native-takeover-size']), (2560, 1440))
                self.assertEqual(layout.journal.read_bytes(), original)
            layout.restore()
            self.assertEqual(snapshot(bus.current), before)

    def test_client_scale_is_mapped_to_a_supported_remote_scale(self):
        self.assertEqual([client_scale(v) for v in (100, 125, 140, 150, 175, 200, 250, 500)],
                         [100, 125, 150, 150, 175, 200, 200, 200])
        for absent in (0, 99, 501, None, True, '125'):
            self.assertIsNone(client_scale(absent))

    def test_new_lease_prefers_the_client_scale_over_the_saved_one(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_CONFIG_HOME': directory}):
            save_scale(150)
            layout = MonitorLayout(FakeBus(), '/unused')
            layout.begin_scale(125)
            self.assertEqual(layout.scale_percent, 125)
            layout.begin_scale()  # The next client announces no scale.
            self.assertEqual(layout.scale_percent, 150)
            settings_path().write_text('damaged')
            layout.begin_scale()
            self.assertEqual(layout.scale_percent, 150)
        greeter = MonitorLayout(FakeBus(), '/unused', scale_percent=100)
        greeter.begin_scale(175)
        self.assertEqual(greeter.scale_percent, 175)
        greeter.begin_scale()
        self.assertEqual(greeter.scale_percent, 100)

    def test_resize_keeps_a_scale_changed_in_gnome_settings(self):
        bus = FakeBus()
        bus.current[3].update({'native-takeover-viewport-supported': True,
                               'native-takeover-scale-supported': True})
        with tempfile.TemporaryDirectory() as directory:
            layout = MonitorLayout(bus, Path(directory) / 'layout.json', scale_percent=100)
            layout.enter()
            layout.resize(1920, 1080, scale_percent=125)
            self.assertEqual(bus.current[2][0][2], 1.25)
            # Mutter applied a GNOME Settings request as a viewport scale.
            bus.current[2][0] = bus.current[2][0][:2] + (1.5,) + bus.current[2][0][3:]
            layout.resize(1600, 900)
            self.assertEqual(bus.current[2][0][2], 1.5)
            self.assertEqual(layout.scale_percent, 150)
            layout.resize(1600, 900, scale_percent=200)
            self.assertEqual(bus.current[2][0][2], 2.0)
            layout.restore()
            self.assertIs(bus.calls[-1][3]['native-takeover-release'], True)
            self.assertNotIn('native-takeover-size', bus.calls[-1][3])

    def test_old_compositor_does_not_silently_accept_scaling(self):
        bus = FakeBus()
        bus.current[3]['native-takeover-viewport-supported'] = True
        with self.assertRaisesRegex(RuntimeError, 'new Mutter'):
            remote_monitor(bus.current, 2560, 1440, 150)

    def test_local_preference_never_changes_physical_layout(self):
        bus = FakeBus()
        bus.privacy = 'local'
        bus.current[3]['native-takeover-scale-supported'] = True
        layout = MonitorLayout(bus, '/unused', scale_percent=100)
        layout.set_scale(150)
        self.assertEqual(bus.calls, [])
        self.assertEqual(layout.scale_percent, 150)


class DashboardTests(unittest.TestCase):
    def test_shortcut_upgrade_replaces_only_package_files_and_tracks_them(self):
        import hashlib
        from takeover.remote_settings_ui import install_shortcut
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            desktop = home / '.local/share/applications/ubuntu-native-takeover-settings.desktop'
            desktop.parent.mkdir(parents=True)
            desktop.write_text('old entry\n')
            manifest = home / '.config/ubuntu-native-takeover/install.json'
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({'files': {str(desktop): None}, 'installed_files': {
                str(desktop): hashlib.sha256(b'old entry\n').hexdigest()}}))
            install_shortcut('/opt/ubuntu-native-takeover', home)
            autostart = home / '.config/autostart/ubuntu-native-takeover.desktop'
            self.assertFalse(autostart.exists(), 'takeover must not be enabled by the shortcut')
            autostart.parent.mkdir(parents=True)
            autostart.write_text('Name=\u539f\u751f\n')
            saved = json.loads(manifest.read_text())
            saved['installed_files'][str(autostart)] = hashlib.sha256(autostart.read_bytes()).hexdigest()
            manifest.write_text(json.dumps(saved))
            install_shortcut('/opt/ubuntu-native-takeover', home)
            self.assertIn('Name=Native Desktop Remote Takeover\n', autostart.read_text())
            self.assertIn('systemctl --user start ubuntu-native-takeover.service', autostart.read_text())
            command = home / '.local/bin/ust'
            self.assertIn('Name=UST\n', desktop.read_text())
            self.assertTrue(os.access(command, os.X_OK))
            self.assertIn('/opt/ubuntu-native-takeover/scripts/remote-settings.py "$@"', command.read_text())
            saved = json.loads(manifest.read_text())
            for path in (desktop, command):
                self.assertEqual(saved['installed_files'][str(path)],
                                 hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertIsNone(saved['files'][str(path)])
            install_shortcut('/opt/ubuntu-native-takeover', home)  # Repeating is harmless.
            command.write_text('#!/bin/sh\necho mine\n')
            with self.assertRaisesRegex(RuntimeError, 'not installed by this package'):
                install_shortcut('/opt/ubuntu-native-takeover', home)
            self.assertEqual(command.read_text(), '#!/bin/sh\necho mine\n')

    def test_status_is_described_for_every_service_state(self):
        from takeover.remote_settings_ui import describe
        self.assertEqual(describe(None)[0], 'Service not running')
        self.assertEqual(describe({'remote': False, 'native_state': 'local'})[0], 'No remote session')
        head, detail = describe({'remote': True, 'viewport': [2560, 1440], 'scale_percent': 125,
                                 'remote_only_monitors': 1, 'client_scale': 125})
        self.assertEqual(head, 'Remote session active: 2560 × 1440, 125%, 2 monitors')
        self.assertIn('announced 125%', detail)
        # A service from before the dashboard reports only its three base keys.
        head, detail = describe({'remote': True, 'native_state': 'remote', 'session': '/s'})
        self.assertEqual(head, 'Remote session active: resolution of the local display, 1 monitor')
        self.assertIn('no scale', detail)

    def test_layout_reports_the_live_viewport(self):
        bus = FakeBus()
        bus.current[3].update({'native-takeover-viewport-supported': True,
                               'native-takeover-scale-supported': True})
        with tempfile.TemporaryDirectory() as directory:
            layout = MonitorLayout(bus, Path(directory) / 'layout.json', scale_percent=100)
            self.assertEqual(layout.describe(), {'viewport': None, 'scale_percent': 100,
                'remote_only_monitors': 0, 'scale_supported': True, 'multi_monitor_supported': False})
            layout.enter()
            layout.resize(1920, 1080, scale_percent=175)
            self.assertEqual(layout.describe()['viewport'], [1920, 1080])
            self.assertEqual(layout.describe()['scale_percent'], 175)


class ScaleControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_reports_dashboard_facts_and_survives_a_failing_layout(self):
        desktop = Mock(state=AsyncMock(return_value='remote'))
        layout = Mock()
        layout.describe.return_value = {'viewport': [1920, 1080], 'scale_percent': 125}
        broker = Broker(desktop, '/session', layout)
        broker.busy, broker.client_scale = True, 125
        with tempfile.TemporaryDirectory() as directory:
            server = await asyncio.start_unix_server(broker.handle, path=directory + '/gate')
            try:
                async def status():
                    reader, writer = await asyncio.open_unix_connection(directory + '/gate')
                    writer.write(b'STATUS\n')
                    await writer.drain()
                    result = json.loads(await reader.readline())
                    writer.close()
                    await writer.wait_closed()
                    return result
                self.assertEqual(await status(), {'native_state': 'remote', 'remote': True, 'session': '/session',
                    'viewport': [1920, 1080], 'scale_percent': 125, 'client_scale': 125})
                layout.describe.side_effect = RuntimeError('compositor busy')
                self.assertEqual(await status(), {'native_state': 'remote', 'remote': True, 'session': '/session'})
            finally:
                server.close()
                await server.wait_closed()

    async def test_secondary_control_connection_never_revokes_the_rdp_lease(self):
        desktop = Mock(end=AsyncMock())
        layout = Mock()
        broker = Broker(desktop, '/session', layout)
        broker.busy = True
        with tempfile.TemporaryDirectory() as directory, patch('takeover.native_broker.save_scale') as save:
            server = await asyncio.start_unix_server(broker.handle, path=directory + '/gate')
            try:
                reader, writer = await asyncio.open_unix_connection(directory + '/gate')
                writer.write(b'SCALE 150\n')
                await writer.drain()
                result = json.loads(await reader.readline())
                self.assertTrue(result['ok'])
                writer.close()
                await writer.wait_closed()
                layout.set_scale.assert_called_once_with(150)
                save.assert_called_once_with(150)
                desktop.end.assert_not_awaited()
                self.assertTrue(broker.busy)
            finally:
                server.close()
                await server.wait_closed()
