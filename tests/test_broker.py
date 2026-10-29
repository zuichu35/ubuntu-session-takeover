"""Exercise actual Unix leases and child processes; mock only physical lock."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest

from takeover.broker import Broker


class PhysicalDesktop:
    def __init__(self):
        self.is_locked = False
        self.lock_failure = False
        self.events = []

    async def lock(self):
        self.events.append('lock')
        if self.lock_failure:
            raise RuntimeError('lock failed')
        self.is_locked = True

    async def locked(self):
        return self.is_locked

    async def available_unlocked(self):
        return not self.is_locked

    def viewer_env(self):
        return dict(os.environ)


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.socket = str(Path(self.tmp.name) / 'broker.sock')
        self.desktop = PhysicalDesktop()
        self.broker = Broker(self.desktop, ['/bin/sleep', '120'])
        self.server = await asyncio.start_unix_server(self.broker.handle, path=self.socket, limit=32)
        self.clients = []

    async def asyncTearDown(self):
        for _, writer in self.clients:
            writer.close()
            await writer.wait_closed()
        await asyncio.sleep(.05)
        await self.broker.stop_viewer()
        self.server.close()
        await self.server.wait_closed()
        self.tmp.cleanup()

    async def connect(self, request):
        reader, writer = await asyncio.open_unix_connection(self.socket)
        self.clients.append((reader, writer))
        writer.write(request + b'\n')
        await writer.drain()
        reply = await asyncio.wait_for(reader.readline(), 1)
        return reader, writer, reply

    async def test_remote_grant_kills_local_viewer_first(self):
        await self.broker.local()
        child = self.broker.viewer
        _, writer, reply = await self.connect(b'REMOTE')
        self.assertEqual(reply, b'GRANTED\n')
        self.assertIsNotNone(child.returncode)
        self.assertTrue(self.desktop.is_locked)
        self.assertIsNone(self.broker.viewer)
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(.05)
        self.assertIsNone(self.broker.remote)
        self.assertTrue(self.desktop.is_locked)

    async def test_lock_failure_denies_remote(self):
        self.desktop.lock_failure = True
        _, _, reply = await self.connect(b'REMOTE')
        self.assertEqual(reply, b'DENIED!\n')
        self.assertIsNone(self.broker.remote)

    async def test_second_remote_cannot_share(self):
        _, _, reply = await self.connect(b'REMOTE')
        self.assertEqual(reply, b'GRANTED\n')
        _, _, reply = await self.connect(b'REMOTE')
        self.assertEqual(reply, b'DENIED!\n')

    async def test_local_cannot_attach_while_locked(self):
        self.desktop.is_locked = True
        _, _, reply = await self.connect(b'LOCAL')
        self.assertEqual(reply, b'DENIED!\n')
        self.assertIsNone(self.broker.viewer)

    async def test_local_waits_for_remote_teardown_ack(self):
        reader, writer, reply = await self.connect(b'REMOTE')
        self.assertEqual(reply, b'GRANTED\n')
        self.desktop.is_locked = False  # GNOME authentication completed.
        acquire = asyncio.create_task(self.broker.local())
        self.assertEqual(await asyncio.wait_for(reader.readline(), 1), b'REVOKE\n')
        self.assertIsNone(self.broker.viewer)
        self.assertFalse(acquire.done())
        writer.close()  # GRD acknowledges only after closing its input/capture.
        await writer.wait_closed()
        await asyncio.wait_for(acquire, 1)
        self.assertIsNotNone(self.broker.viewer)
        self.assertIsNone(self.broker.remote)

    async def test_lock_during_remote_revocation_does_not_open_local(self):
        reader, writer, _ = await self.connect(b'REMOTE')
        self.desktop.is_locked = False
        acquire = asyncio.create_task(self.broker.local())
        await asyncio.wait_for(reader.readline(), 1)
        self.desktop.is_locked = True
        writer.close()
        await writer.wait_closed()
        with self.assertRaisesRegex(RuntimeError, 'locked during'):
            await acquire
        self.assertIsNone(self.broker.viewer)

    async def test_crashed_viewer_retries_only_while_unlocked(self):
        await self.broker.local()
        original = self.broker.viewer
        original.kill()
        await original.wait()
        await self.broker.monitor_once()
        self.assertIsNone(self.broker.viewer)
        self.assertEqual(self.broker.viewer_failures, 1)
        self.broker.viewer_retry_at = 0
        self.desktop.is_locked = True
        await self.broker.monitor_once()
        self.assertIsNone(self.broker.viewer)
        self.desktop.is_locked = False
        await self.broker.monitor_once()
        self.assertIsNotNone(self.broker.viewer)
        self.assertNotEqual(original.pid, self.broker.viewer.pid)

    async def test_intentional_close_waits_for_next_unlock(self):
        self.broker.viewer_command = ['/usr/bin/true']
        await self.broker.local()
        await self.broker.viewer.wait()
        self.broker.viewer_command = ['/bin/sleep', '120']
        await self.broker.monitor_once()
        self.assertTrue(self.broker.local_paused)
        self.assertIsNone(self.broker.viewer)
        await self.broker.monitor_once()
        self.assertIsNone(self.broker.viewer)
        self.desktop.is_locked = True
        await self.broker.monitor_once()
        self.desktop.is_locked = False
        await self.broker.monitor_once()
        self.assertFalse(self.broker.local_paused)
        self.assertIsNotNone(self.broker.viewer)


if __name__ == '__main__':
    unittest.main()
