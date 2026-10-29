import copy
import tempfile
from pathlib import Path
import unittest
from gi.repository import GLib
from takeover.monitor_layout import MonitorLayout, arguments, snapshot, single_monitor, adaptive_monitor, mirrored_monitor, remote_monitor


def initial_state():
    specs = [('DP-3', 'A', 'Main', '1'), ('HDMI-4', 'B', 'Side', '2')]
    monitors = [(s, [('1920x1080@60', 1920, 1080, 60., 1., [1., 1.5, 2.],
                      {'is-current': True})], {'color-mode': 0}) for s in specs]
    logical = [(0, 0, 2., 0, True, [specs[0]], {}),
               (960, 0, 1.5, 1, False, [specs[1]], {})]
    return [7, monitors, logical, {'layout-mode': 1, 'supports-changing-layout-mode': True}]


class FakeBus:
    def __init__(self):
        self.current = initial_state()
        self.privacy = 'remote'
        self.calls = []
        self.fail_apply = False

    def call_sync(self, name, path, interface, method, args, *rest):
        if method == 'GetState':
            return GLib.Variant('(s)', (self.privacy,))
        if method == 'GetCurrentState':
            return type('Reply', (), {'unpack': lambda _: copy.deepcopy(self.current)})()
        serial, how, logical, props = args.unpack()
        self.calls.append((serial, how, logical, props))
        if serial != self.current[0]:
            raise RuntimeError('stale serial')
        if how == 1:
            if self.fail_apply:
                raise RuntimeError('apply failure')
            by_name = {s[0]: s for s, _, _ in self.current[1]}
            self.current[2] = [(x, y, scale, transform, primary,
                                [by_name[c] for c, _, _ in monitors], {})
                               for x, y, scale, transform, primary, monitors in logical]
            self.current[0] += 1
            self.current[3].pop('native-takeover-size', None)
            self.current[3].update(props)
            for group in logical:
                for connector, mode, _ in group[5]:
                    for spec, modes, _ in self.current[1]:
                        if spec[0] == connector:
                            for m in modes:
                                m[6]['is-current'] = m[0] == mode


class MonitorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'monitor-layout.json'
        self.bus = FakeBus()
        self.layout = MonitorLayout(self.bus, self.path)

    def test_adaptive_mode_and_original_scale_restore(self):
        modes = self.bus.current[1][0][1]
        modes.append(('1280x720@60', 1280, 720, 60., 1., [1.], {}))
        self.bus.current[1][1][1].append(('side-720', 1280, 720, 60., 1., [1.], {}))
        self.layout.enter()
        original = self.path.read_bytes()
        self.layout.resize(1280, 720)
        self.assertEqual(snapshot(self.bus.current)['logical'][0][5][0][1], '1280x720@60')
        self.assertEqual(self.bus.current[2][0][2], 1.)
        self.assertEqual(self.path.read_bytes(), original)
        count = len(self.bus.calls)
        self.layout.resize(1280, 720)
        self.assertEqual(len(self.bus.calls), count)
        self.layout.restore()
        self.assertEqual(snapshot(self.bus.current)['logical'][0][5][0][1], '1920x1080@60')
        self.assertEqual(self.bus.current[2][0][2], 2.)

    def test_nearest_and_portrait_modes_and_invalid_size(self):
        for size, expected in [((1900, 1060), (1920, 1080)), ((1080, 1920), (1080, 1920))]:
            _, chosen = adaptive_monitor(self.bus.current, *size)
            self.assertEqual(chosen, expected)
        for size in [(0, 720), (1920, 100000)]:
            with self.assertRaises(ValueError):
                adaptive_monitor(self.bus.current, *size)

    def test_round_trip_preserves_scale_rotation_primary_and_uses_fresh_serial(self):
        before = copy.deepcopy(self.bus.current[2])
        self.layout.enter()
        self.assertEqual(len(self.bus.current[2]), 1)
        self.assertEqual(len(self.bus.current[2][0][5]), 2)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.layout.restore()
        self.assertEqual(self.bus.current[2], before)
        self.assertFalse(self.path.exists())
        self.assertEqual([c[1] for c in self.bus.calls], [0, 1, 1])
        self.assertEqual([c[0] for c in self.bus.calls], [7, 7, 8])

    def test_unprotected_local_desktop_is_never_changed(self):
        self.bus.privacy = 'local'
        with self.assertRaisesRegex(RuntimeError, 'privacy'):
            self.layout.enter()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.bus.calls, [])

    def test_failed_apply_keeps_journal_and_can_recover(self):
        self.bus.fail_apply = True
        with self.assertRaisesRegex(RuntimeError, 'apply failure'):
            self.layout.enter()
        self.assertTrue(self.path.exists())
        self.bus.fail_apply = False
        # A new process restores the original snapshot, not the current layout.
        MonitorLayout(self.bus, self.path).restore()
        self.assertFalse(self.path.exists())

    def test_hot_unplug_keeps_recovery_data_without_partial_restore(self):
        self.layout.enter()
        self.bus.current[1].pop()
        count = len(self.bus.calls)
        with self.assertRaisesRegex(RuntimeError, 'Monitor changed'):
            self.layout.restore()
        self.assertEqual(len(self.bus.calls), count)
        self.assertTrue(self.path.exists())

    def test_existing_snapshot_is_not_overwritten(self):
        self.layout.enter()
        original = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Previous monitor'):
            self.layout.enter()
        self.assertEqual(self.path.read_bytes(), original)

    def test_single_monitor_is_noop(self):
        self.bus.current[2].pop()
        self.layout.enter()
        self.layout.restore()
        self.assertEqual(self.bus.calls, [])

    def test_no_common_mode_never_disables_a_display(self):
        self.bus.current[1][1][1][0] = ('side-only', 1024, 768, 60., 1., [1.], {'is-current': True})
        with self.assertRaisesRegex(RuntimeError, 'no common mirror mode'):
            self.layout.enter()
        self.assertEqual(self.bus.calls, [])
        self.assertFalse(self.path.exists())

    def test_adaptive_mirror_keeps_every_connector(self):
        target, actual = mirrored_monitor(self.bus.current, 1280, 720)
        self.assertEqual(actual, (1920, 1080))
        self.assertEqual({o[0] for o in target['logical'][0][5]}, {'DP-3', 'HDMI-4'})

    def test_exact_viewport_keeps_unequal_hardware_modes_and_restores(self):
        self.bus.current[3]['native-takeover-viewport-supported'] = True
        self.bus.current[1][1][1][0] = ('side-only', 1024, 768, 60., 1., [1.], {'is-current': True})
        original = snapshot(self.bus.current)
        self.layout.enter()
        journal = self.path.read_bytes()
        for size in [(2560, 1440), (3024, 1964), (1000, 600)]:
            self.layout.resize(*size)
            self.assertEqual(tuple(self.bus.current[3]['native-takeover-size']), size)
            self.assertEqual(len(self.bus.current[2][0][5]), 2)
            self.assertEqual(self.path.read_bytes(), journal)
        count = len(self.bus.calls)
        self.layout.resize(1000, 600)
        self.assertEqual(len(self.bus.calls), count)
        self.layout.restore()
        self.assertEqual(snapshot(self.bus.current), original)

    def test_exact_viewport_needs_capability_and_rejects_invalid_size(self):
        self.assertEqual(remote_monitor(self.bus.current, 2560, 1440)[1], (1920, 1080))
        self.bus.current[3]['native-takeover-viewport-supported'] = True
        for size in [(0, 600), (8193, 600), (2560, -1)]:
            with self.assertRaises(ValueError):
                remote_monitor(self.bus.current, *size)


if __name__ == '__main__':
    unittest.main()
