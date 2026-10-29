import asyncio
import unittest
from unittest.mock import patch
from takeover.native_broker import NativeDesktop


class Desktop(NativeDesktop):
    def __init__(self, mode):
        self.connection = object()
        self.revoked = asyncio.Event()
        self.woken = asyncio.Event()
        self.mode = mode
        self.current = 'local'
        self.events = []
        self.fail_lock = self.fail_begin = self.fail_wake = False

    async def confirm_lock(self, session):
        self.events.append('lock-confirmed')
        if self.fail_lock:
            raise RuntimeError('lock failure')

    async def state(self):
        return self.current

    async def call(self, method, params=None, timeout=7000):
        assert method == 'Begin'
        self.events.append('begin')
        if self.fail_begin:
            raise RuntimeError('begin refused')
        self.current = 'blanking'
        if self.mode > 0:
            await self.woken.wait()
        self.current = 'remote'
        self.events.append('black-frame-confirmed')
        return ()

    async def rpc(self, bus, name, path, interface, method, params=None, timeout=3000):
        if method == 'Get':
            return (self.mode,)
        assert method == 'Set'
        assert self.current == 'blanking'
        self.events.append('wake')
        if self.fail_wake:
            raise RuntimeError('wake failed')
        self.woken.set()
        return ()


class PowerSaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_independent_lock_indications_are_required(self):
        desktop = NativeDesktop.__new__(NativeDesktop)
        desktop.connection = object()
        active = iter([False, True, True])
        locked = iter([True, False, True])
        methods = []
        async def rpc(bus, name, path, interface, method, *args, **kwargs):
            methods.append(method)
            return () if method == 'Lock' else ((next(active),) if method == 'GetActive' else (next(locked),))
        desktop.rpc = rpc
        with patch('takeover.native_broker.Gio.bus_get_sync', return_value=object()):
            await desktop.confirm_lock('/session')
        self.assertEqual(methods.count('GetActive'), 3)
        self.assertEqual(methods.count('Get'), 3)
        self.assertIn('Lock', methods)

    async def test_all_sleep_modes_lock_and_isolate_before_wake(self):
        for mode in (1, 2, 3):
            desktop = Desktop(mode)
            await desktop.begin('/session')
            self.assertEqual(desktop.events,
                             ['lock-confirmed', 'begin', 'wake', 'black-frame-confirmed'])
            self.assertEqual(await desktop.state(), 'remote')

    async def test_on_or_unsupported_does_not_change_power_or_lock(self):
        for mode in (0, -1):
            desktop = Desktop(mode)
            await desktop.begin('/session')
            self.assertEqual(desktop.events, ['begin', 'black-frame-confirmed'])

    async def test_lock_failure_never_starts_or_wakes(self):
        desktop = Desktop(3)
        desktop.fail_lock = True
        with self.assertRaisesRegex(RuntimeError, 'lock failure'):
            await desktop.begin('/session')
        self.assertEqual(desktop.events, ['lock-confirmed'])

    async def test_refused_begin_never_wakes(self):
        desktop = Desktop(3)
        desktop.fail_begin = True
        with self.assertRaisesRegex(RuntimeError, 'begin refused'):
            await desktop.begin('/session')
        self.assertNotIn('wake', desktop.events)

    async def test_wake_failure_cannot_grant(self):
        desktop = Desktop(3)
        desktop.fail_wake = True
        with self.assertRaisesRegex(RuntimeError, 'wake failed'):
            await desktop.begin('/session')
        self.assertNotIn('black-frame-confirmed', desktop.events)


if __name__ == '__main__':
    unittest.main()
