"""Regression tests for the real GNOME lock confirmation path."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch
from takeover.broker import LocalDesktop


class LockTests(unittest.IsolatedAsyncioTestCase):
    def desktop(self):
        d = LocalDesktop('/unused')
        d.config = lambda: {'bus': 'unix:path=/unused'}
        d.session = AsyncMock(return_value={'LockedHint': 'yes'})
        d.screen_active = AsyncMock(return_value=True)
        return d

    async def test_already_locked_does_not_wait_for_a_second_transition(self):
        d = self.desktop()
        with patch('takeover.broker.command', new_callable=AsyncMock) as command:
            await asyncio.wait_for(d.lock(), .5)
            command.assert_not_called()

    async def test_confirmation_requires_both_gnome_and_logind(self):
        d = self.desktop()
        d.screen_active.return_value = False
        self.assertFalse(await d.locked())
        d.session.return_value = {'LockedHint': 'no'}
        d.screen_active.return_value = True
        self.assertFalse(await d.locked())

    async def test_lock_observed_even_if_dbus_reply_does_not_finish(self):
        d = self.desktop()
        d.locked = AsyncMock(side_effect=[False, False, True])
        cancelled = asyncio.Event()
        async def pending(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch('takeover.broker.command', pending):
            await asyncio.wait_for(d.lock(), 1)
        self.assertTrue(cancelled.is_set())

    async def test_logged_out_closing_session_does_not_block_remote(self):
        import os
        d = LocalDesktop('/unused')
        d.config = lambda: {'session': 'old'}
        async def replies(*args):
            if args[1] == 'list-sessions':
                return json.dumps([{'session': 'old', 'uid': os.getuid(), 'seat': 'seat0'},
                                   {'session': 'tty', 'uid': os.getuid(), 'seat': 'seat0'}])
            if args[2] == 'old':
                return 'State=closing\nType=wayland'
            return 'State=online\nType=tty'
        with patch('takeover.broker.command', replies):
            self.assertIsNone(await d.session())

    async def test_unregistered_new_physical_session_fails_closed(self):
        import os
        d = LocalDesktop('/unused')
        d.config = lambda: {'session': 'old'}
        async def replies(*args):
            if args[1] == 'list-sessions':
                return json.dumps([{'session': 'new', 'uid': os.getuid(), 'seat': 'seat0'}])
            return 'State=active\nType=wayland'
        with patch('takeover.broker.command', replies):
            with self.assertRaisesRegex(RuntimeError, 'physical session changed'):
                await d.session()


if __name__ == '__main__':
    unittest.main()
