import asyncio
import unittest
from unittest.mock import AsyncMock, Mock
from takeover.native_broker import Broker, resolution_request


class ResizeProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_coalesced_resize_does_not_release_lease(self):
        desktop = Mock(revoked=asyncio.Event(), state=AsyncMock(return_value='remote'))
        layout = Mock()
        broker = Broker(desktop, '/session', layout)
        reader = asyncio.StreamReader()
        task = asyncio.create_task(broker.read_updates(reader))
        reader.feed_data(b'RESIZE 1280 720\nRESIZE 1920 1080\n')
        await asyncio.sleep(.2)
        layout.resize.assert_called_once_with(1920, 1080)
        self.assertFalse(task.done())
        reader.feed_eof()
        await task

    async def test_disconnect_discards_pending_resize(self):
        desktop = Mock(revoked=asyncio.Event(), state=AsyncMock(return_value='remote'))
        layout = Mock()
        reader = asyncio.StreamReader()
        reader.feed_data(b'RESIZE 1920 1080\n')
        reader.feed_eof()
        await Broker(desktop, '/session', layout).read_updates(reader)
        layout.resize.assert_not_called()

    async def test_revoked_session_cannot_resize(self):
        desktop = Mock(revoked=asyncio.Event(), state=AsyncMock(return_value='remote'))
        desktop.revoked.set()
        layout = Mock()
        reader = asyncio.StreamReader()
        task = asyncio.create_task(Broker(desktop, '/session', layout).read_updates(reader))
        reader.feed_data(b'RESIZE 1920 1080\n')
        await asyncio.sleep(.2)
        reader.feed_eof()
        await task
        layout.resize.assert_not_called()

    async def test_invalid_dimensions_rejected(self):
        for request in [b'RESIZE 0 720', b'RESIZE -1 720', b'RESIZE 9000 1000', b'RESIZE nan 900']:
            with self.assertRaises(ValueError):
                resolution_request(request, b'RESIZE')


if __name__ == '__main__':
    unittest.main()
