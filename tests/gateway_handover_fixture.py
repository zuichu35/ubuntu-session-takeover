"""Production gateway in a private test network, with explicit fake logind.

The RDP servers and client still perform real authentication/redirect handshakes.
Only session discovery and registration metadata are injected; no host bus is
queried and no production port is bound outside the enclosing private namespace.
"""
import asyncio
import atexit
import contextlib
import json
import os
from pathlib import Path
import sys
import threading


class RoutedEndpoints(dict):
    def __setitem__(self, role, endpoint):
        super().__setitem__(role, (*endpoint[:2], 43394 if role == 'greeter' else 43393))


class ProductionGatewayFixture:
    def __init__(self, runtime_root, directory):
        sys.path.insert(0, str(runtime_root))
        from takeover.login_gateway import Gateway
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.directory / 'control.sock'
        self.source_session = '/org/freedesktop/login1/session/test'
        self.target_session = '/org/freedesktop/login1/session/target'
        self.target_created = threading.Event()
        self.target_locked = False
        self.ready = threading.Event()
        self.error = None
        self.registrations = {}
        def properties(path):
            if path not in (self.source_session, self.target_session):
                raise ValueError('Unknown isolated logind session')
            return {'User': [os.getuid(), '/test-user'], 'Seat': ['seat0', '/test-seat'],
                    'Type': 'wayland', 'Remote': False, 'State': 'active', 'Active': True,
                    'Class': 'greeter' if path == self.source_session else 'user',
                    'LockedHint': self.target_locked if path == self.target_session else False}
        self.gateway = Gateway(self.directory, os.getuid(), properties,
            self.target_created.is_set, lambda uid, props: uid == os.getuid(),
            target_session=lambda: self.target_session if self.target_created.is_set() else None,
            handover_timeout=30)
        self.gateway.endpoints = RoutedEndpoints()
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()
        if not self.ready.wait(5):
            raise RuntimeError('Isolated production gateway did not start')
        if self.error:
            raise self.error
        atexit.register(self.close)

    def run(self):
        try:
            asyncio.run(self.serve())
        except Exception as error:
            self.error = error
            self.ready.set()

    async def serve(self):
        self.loop = asyncio.get_running_loop()
        self.stopping = asyncio.Event()
        control = await asyncio.start_unix_server(self.gateway.control, path=str(self.path))
        relay = await asyncio.start_server(self.gateway.relay, '127.0.0.1', 43392)
        self.ready.set()
        try:
            await self.stopping.wait()
        finally:
            for _, writer in self.registrations.values():
                writer.close()
            await self.gateway.shutdown(control, relay)
            self.path.unlink(missing_ok=True)

    async def register(self, role, session):
        reader, writer = await asyncio.open_unix_connection(str(self.path))
        writer.write((json.dumps({'command': 'register', 'role': role, 'session': session}) + '\n').encode())
        await writer.drain()
        response = await asyncio.wait_for(reader.readline(), 3)
        if response != b'READY\n':
            writer.close()
            raise RuntimeError('Isolated endpoint registration was refused: ' + response.decode())
        self.registrations[role] = reader, writer

    def register_source(self):
        asyncio.run_coroutine_threadsafe(self.register('greeter', self.source_session), self.loop).result(5)

    def create_target_session(self):
        self.target_created.set()

    def register_target(self):
        self.target_locked = True  # Explicit fake logind fixture; not real GDM/PAM evidence.
        asyncio.run_coroutine_threadsafe(self.register('desktop', self.target_session), self.loop).result(5)

    def remove_source(self):
        async def remove():
            _, writer = self.registrations.pop('greeter')
            writer.close()
            await writer.wait_closed()
        asyncio.run_coroutine_threadsafe(remove(), self.loop).result(5)

    def close(self):
        if self.worker.is_alive() and hasattr(self, 'loop'):
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self.stopping.set)
            self.worker.join(3)
