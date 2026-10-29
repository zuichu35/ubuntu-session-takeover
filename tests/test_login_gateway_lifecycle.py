"""Real socket/SIGTERM coverage; all ports and directories are isolated."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from takeover.login_gateway import Gateway


ROOT = Path(__file__).resolve().parents[1]


def properties(path):
    return {'User': [os.getuid(), '/user'], 'Seat': ['seat0', '/seat'],
            'Type': 'wayland', 'Class': 'greeter', 'Remote': False,
            'LockedHint': True}


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_registration_lease_and_relay_close_without_client_eof(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway = Gateway(directory, os.getuid(), properties, lambda: False, lambda uid, props: True,
                              target_session=lambda: None)
            path = str(Path(directory) / 'control.sock')
            control = await asyncio.start_unix_server(gateway.control, path=path)
            relay = await asyncio.start_server(gateway.relay, '127.0.0.1', 0)
            backend_closed = asyncio.Event()

            async def echo(reader, writer):
                try:
                    while data := await reader.read(4096):
                        writer.write(data)
                        await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()
                    backend_closed.set()

            backend = await asyncio.start_server(echo, '127.0.0.1', 0)
            clients = []
            try:
                async def request(command):
                    reader, writer = await asyncio.open_unix_connection(path)
                    clients.append(writer)
                    writer.write((json.dumps({'command': command, 'role': 'greeter', 'session': '/greeter'}) + '\n').encode())
                    await writer.drain()
                    return reader, await asyncio.wait_for(reader.readline(), 1)

                registration, reply = await request('register')
                self.assertEqual(reply, b'READY\n')
                sequence, session, _ = gateway.endpoints['greeter']
                gateway.endpoints['greeter'] = (sequence, session, backend.sockets[0].getsockname()[1])
                lease, reply = await request('lease')
                self.assertEqual(reply, b'GUARDED\n')
                guard = Path(directory) / 'boot-guard'
                marker = guard.read_bytes()
                remote, writer = await asyncio.open_connection('127.0.0.1', relay.sockets[0].getsockname()[1])
                clients.append(writer)
                writer.write(b'ping')
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(remote.readexactly(4), 1), b'ping')

                started = time.monotonic()
                await asyncio.wait_for(gateway.shutdown(control, relay), 2)
                self.assertLess(time.monotonic() - started, 2)
                for reader in (registration, lease, remote):
                    self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
                await asyncio.wait_for(backend_closed.wait(), 1)
                self.assertEqual(gateway.endpoints, {})
                self.assertIsNone(gateway.greeter_lease)
                self.assertEqual(guard.read_bytes(), marker)
                self.assertTrue(all(task.done() for task in gateway.client_tasks))
                self.assertFalse(gateway.client_writers)

                # An equivalent new gateway can accept the same supervisor
                # immediately after EOF, without a compositor/backend restart.
                fresh = Gateway(directory, os.getuid(), properties, lambda: False, lambda uid, props: True,
                                target_session=lambda: None)
                replacement = await asyncio.start_unix_server(fresh.control, path=path)
                try:
                    _, reply = await request('register')
                    self.assertEqual(reply, b'READY\n')
                    self.assertIn('greeter', fresh.endpoints)
                finally:
                    await fresh.shutdown(replacement)
            finally:
                await gateway.shutdown(control, relay)
                for writer in clients:
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
                backend.close()
                await backend.wait_closed()


LEGACY_CHILD = r'''
import asyncio,signal,sys
async def main():
    async def client(reader,writer):
        writer.write(b'READY\n');await writer.drain()
        await reader.read()
        writer.close();await writer.wait_closed()
    server=await asyncio.start_unix_server(client,path=sys.argv[1])
    stopping=asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,stopping.set)
    print('LISTENING',flush=True)
    async with server:
        await stopping.wait()
asyncio.run(main())
'''

FIXED_CHILD = r'''
import asyncio,contextlib,os,sys
from pathlib import Path
from takeover.login_gateway import Gateway,serve
async def main():
    directory=Path(sys.argv[1])
    def properties(path):
        return {'User':[os.getuid(),'/user'],'Seat':['seat0','/seat'],
                'Type':'wayland','Class':'greeter','Remote':False,'LockedHint':True}
    async def echo(reader,writer):
        try:
            while data:=await reader.read(4096):
                writer.write(data);await writer.drain()
        finally:
            writer.close();await writer.wait_closed()
    backend=await asyncio.start_server(echo,'127.0.0.1',0)
    gateway=Gateway(directory,os.getuid(),properties,lambda:False,lambda uid,props:True,target_session=lambda:None)
    async def isolated_backend():
        while 'greeter' not in gateway.endpoints:
            await asyncio.sleep(.005)
        sequence,session,_=gateway.endpoints['greeter']
        gateway.endpoints['greeter']=(sequence,session,backend.sockets[0].getsockname()[1])
        print('TEST_BACKEND_READY',flush=True)
    watcher=asyncio.create_task(isolated_backend())
    try:
        await serve(gateway,directory/'control.sock',host='127.0.0.1',port=0)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher,return_exceptions=True)
        backend.close();await backend.wait_closed()
asyncio.run(main())
'''

SLOW_QUERY_CHILD = r'''
import asyncio,os,sys,time
from pathlib import Path
from takeover.login_gateway import Gateway,serve
def blocked():
    print('QUERY_BLOCKED',flush=True)
    time.sleep(30)
    return None
async def main():
    gateway=Gateway(Path(sys.argv[1]),os.getuid(),target_session=blocked)
    await serve(gateway,Path(sys.argv[1])/'control.sock',host='127.0.0.1',port=0)
asyncio.run(main())
'''

SLOW_VALIDATION_CHILD = r'''
import asyncio,os,sys,time
from pathlib import Path
from takeover.login_gateway import Gateway,serve
def blocked(path):
    print('QUERY_BLOCKED',flush=True)
    time.sleep(30)
    return {}
async def main():
    gateway=Gateway(Path(sys.argv[1]),os.getuid(),session_info=blocked,target_session=lambda:None)
    await serve(gateway,Path(sys.argv[1])/'control.sock',host='127.0.0.1',port=0)
asyncio.run(main())
'''


class SignalShutdownTests(unittest.TestCase):
    def start_child(self, code, argument):
        child = subprocess.Popen([sys.executable, '-u', '-c', code, str(argument)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_child, child)
        return child

    @staticmethod
    def stop_child(child):
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=2)

    def line(self, child):
        self.assertTrue(select.select([child.stdout], [], [], 2)[0], 'Child did not become ready')
        return child.stdout.readline().strip()

    @unittest.skipUnless(sys.version_info >= (3, 12, 1), 'Requires the fixed asyncio Server.wait_closed semantics')
    def test_legacy_server_context_exit_waits_for_registration_eof(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'control.sock'
            child = self.start_child(LEGACY_CHILD, path)
            self.assertEqual(self.line(child), 'LISTENING')
            with socket.socket(socket.AF_UNIX) as registration:
                registration.settimeout(1)
                registration.connect(str(path))
                self.assertEqual(registration.recv(64), b'READY\n')
                child.send_signal(signal.SIGTERM)
                with self.assertRaises(subprocess.TimeoutExpired):
                    child.wait(timeout=.25)
            # Releasing the registration, without another signal, unblocks the
            # old process. This establishes the shutdown dependency cycle.
            self.assertEqual(child.wait(timeout=1), 0)

    def test_real_sigterm_closes_live_clients_and_cleans_socket_within_two_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            child = self.start_child(FIXED_CHILD, directory)
            ready = self.line(child)
            self.assertTrue(ready.startswith('Native login gateway listening on '), ready)
            port = int(ready.rsplit(' ', 1)[1])
            path = Path(directory) / 'control.sock'
            sockets = []
            try:
                def request(command):
                    client = socket.socket(socket.AF_UNIX)
                    client.settimeout(1)
                    client.connect(str(path))
                    sockets.append(client)
                    client.sendall((json.dumps({'command': command, 'role': 'greeter', 'session': '/greeter'}) + '\n').encode())
                    return client, client.recv(64)
                registration, reply = request('register')
                self.assertEqual(reply, b'READY\n')
                self.assertEqual(self.line(child), 'TEST_BACKEND_READY')
                lease, reply = request('lease')
                self.assertEqual(reply, b'GUARDED\n')
                guard = Path(directory) / 'boot-guard'
                marker = guard.read_bytes()
                remote = socket.create_connection(('127.0.0.1', port), timeout=1)
                sockets.append(remote)
                remote.sendall(b'ping')
                self.assertEqual(remote.recv(64), b'ping')
                started = time.monotonic()
                child.send_signal(signal.SIGTERM)
                self.assertEqual(child.wait(timeout=2), 0, child.stderr.read())
                self.assertLess(time.monotonic() - started, 2)
                for client in (registration, lease, remote):
                    self.assertEqual(client.recv(64), b'')
                self.assertFalse(path.exists())
                self.assertEqual(guard.read_bytes(), marker)
            finally:
                for client in sockets:
                    client.close()

    def test_real_sigterm_does_not_join_a_blocked_discovery_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            child = self.start_child(SLOW_QUERY_CHILD, directory)
            ready = self.line(child)
            port = int(ready.rsplit(' ', 1)[1])
            with socket.create_connection(('127.0.0.1', port), timeout=1) as connection:
                self.assertEqual(self.line(child), 'QUERY_BLOCKED')
                started = time.monotonic()
                child.send_signal(signal.SIGTERM)
                self.assertEqual(child.wait(timeout=2), 0)
                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(connection.recv(64), b'')
            self.assertFalse((Path(directory) / 'control.sock').exists())

    def test_real_sigterm_does_not_wait_for_blocked_endpoint_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            child = self.start_child(SLOW_VALIDATION_CHILD, directory)
            self.line(child)
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(1)
                connection.connect(str(Path(directory) / 'control.sock'))
                connection.sendall(b'{"command":"register","role":"desktop","session":"/desktop"}\n')
                self.assertEqual(self.line(child), 'QUERY_BLOCKED')
                child.send_signal(signal.SIGTERM)
                self.assertEqual(child.wait(timeout=2), 0)
                self.assertEqual(connection.recv(64), b'')


if __name__ == '__main__':
    unittest.main()
