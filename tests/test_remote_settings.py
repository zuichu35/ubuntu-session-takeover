import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from takeover.remote_settings import load_scale, save_scale, settings_path, validate_scale
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


class ScaleControlTests(unittest.IsolatedAsyncioTestCase):
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
