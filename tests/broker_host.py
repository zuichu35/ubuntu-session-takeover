"""Test fixture: production broker, isolated physical-lock model and real viewer."""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from takeover.broker import Broker


class TestPhysicalDesktop:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        return json.loads(self.path.read_text())

    async def lock(self):
        state = self.read()
        if state.get('fail'):
            raise RuntimeError('test lock failure')
        state['locked'] = True
        state['locks'] += 1
        self.path.write_text(json.dumps(state))

    async def locked(self):
        return self.read()['locked']

    async def available_unlocked(self):
        return not await self.locked()

    def viewer_env(self):
        return dict(os.environ)


async def main():
    path, physical, status, *viewer = sys.argv[1:]
    broker = Broker(TestPhysicalDesktop(physical), viewer, status)
    server = await asyncio.start_unix_server(broker.handle, path=path, limit=32)
    os.chmod(path, 0o600)
    broker.publish()
    monitor = asyncio.create_task(broker.monitor())
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        async with server:
            await stop.wait()
    finally:
        monitor.cancel()
        broker.closing = True
        await broker.stop_viewer()
        if broker.remote:
            broker.remote.writer.close()


asyncio.run(main())
