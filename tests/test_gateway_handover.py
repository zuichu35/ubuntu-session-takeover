import asyncio
import contextlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from takeover.login_gateway import Gateway, find_native_desktop_session, has_native_user_session, read_native_status


class RoutedEndpoints(dict):
    def __init__(self, ports):
        super().__init__()
        self.ports = ports

    def __setitem__(self, role, endpoint):
        super().__setitem__(role, (*endpoint[:2], self.ports[role]))


class HandoverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.target = None
        self.locked = False
        self.target_uid = os.getuid()
        self.other_user = False
        self.clients, self.backends, self.gateways = [], [], []
        self.received = {'greeter': [], 'desktop': []}
        self.ports = {}
        self.auth_status = {'state': 'local', 'local-authenticated': True,
                            'has-owner': False, 'remote-inhibited': True,
                            'session-path': '/desktop'}
        for role in ('greeter', 'desktop'):
            async def echo(reader, writer, selected=role):
                try:
                    while data := await reader.read(4096):
                        self.received[selected].append(data)
                        writer.write(selected.encode() + b':' + data)
                        await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()
            server = await asyncio.start_server(echo, '127.0.0.1', 0)
            self.backends.append(server)
            self.ports[role] = server.sockets[0].getsockname()[1]
        self.gateway, self.control, self.relay = await self.start_gateway()

    def properties(self, path):
        if path not in ('/greeter', '/desktop', '/replacement'):
            raise ValueError('Unknown session')
        return {'User': [os.getuid() if path == '/greeter' else self.target_uid, '/user'],
                'Seat': ['seat0', '/seat'], 'Type': 'wayland', 'Remote': False,
                'Class': 'greeter' if path == '/greeter' else 'user', 'Active': True,
                'LockedHint': self.locked}

    async def start_gateway(self):
        gateway = Gateway(self.directory, os.getuid(), self.properties,
            lambda: self.target is not None or self.other_user, lambda uid, props: uid == os.getuid(),
            target_session=lambda: self.target, native_status=lambda uid: dict(self.auth_status),
            handover_timeout=.3, poll_interval=.01, target_cache_ttl=.005)
        gateway.endpoints = RoutedEndpoints(self.ports)
        control = await asyncio.start_unix_server(gateway.control, path=str(self.directory / 'control.sock'))
        relay = await asyncio.start_server(gateway.relay, '127.0.0.1', 0)
        self.gateways.append((gateway, control, relay))
        return gateway, control, relay

    async def asyncTearDown(self):
        for gateway, control, relay in self.gateways:
            await gateway.shutdown(control, relay)
        for writer in self.clients:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        for backend in self.backends:
            backend.close()
            await backend.wait_closed()
        self.temporary.cleanup()

    async def request(self, command, role='desktop', session='/desktop', **extra):
        reader, writer = await asyncio.open_unix_connection(str(self.directory / 'control.sock'))
        self.clients.append(writer)
        writer.write((json.dumps(dict(command=command, role=role, session=session, **extra)) + '\n').encode())
        await writer.drain()
        return reader, writer, await asyncio.wait_for(reader.readline(), 1)

    async def connect(self, relay=None):
        relay = relay or self.relay
        reader, writer = await asyncio.open_connection('127.0.0.1', relay.sockets[0].getsockname()[1])
        self.clients.append(writer)
        return reader, writer

    async def until(self, predicate):
        async with asyncio.timeout(1):
            while not predicate():
                await asyncio.sleep(.005)

    async def lease(self):
        _, registration, reply = await self.request('register', 'greeter', '/greeter')
        self.assertEqual(reply, b'READY\n')
        reader, writer, reply = await self.request('lease', 'greeter', '/greeter')
        self.assertEqual(reply, b'GUARDED\n')
        return reader, writer, registration

    async def test_redirect_is_early_once_and_guard_remains_until_locked_target_ready(self):
        source, _, _ = await self.lease()
        self.target = '/desktop'
        self.assertEqual(await asyncio.wait_for(source.readline(), 1), b'HANDOVER\n')
        self.assertNotIn('desktop', self.gateway.endpoints)
        self.assertTrue((self.directory / 'boot-guard').exists())
        _, _, reply = await self.request('register')
        self.assertIn(b'must be locked', reply)
        self.locked = True
        _, _, reply = await self.request('register')
        self.assertEqual(reply, b'READY\n')
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(source.readline(), .04)
        self.assertFalse((self.directory / 'boot-guard').exists())

    async def test_fresh_connection_waits_after_source_exit_and_forwards_initial_bytes_once(self):
        source, lease, registration = await self.lease()
        self.target = '/desktop'
        self.assertEqual(await asyncio.wait_for(source.readline(), 1), b'HANDOVER\n')
        lease.close()
        registration.close()
        await self.until(lambda: self.gateway.greeter_lease is None and 'greeter' not in self.gateway.endpoints)
        reader, writer = await self.connect()
        writer.write(b'fresh-TLS-handshake')
        await writer.drain()
        await self.until(lambda: self.gateway.waiting_relays == 1)
        self.assertEqual(self.received['desktop'], [])
        self.locked = True
        self.assertEqual((await self.request('register'))[2], b'READY\n')
        self.assertEqual(await asyncio.wait_for(reader.readexactly(27), 1), b'desktop:fresh-TLS-handshake')
        self.assertEqual(b''.join(self.received['desktop']), b'fresh-TLS-handshake')

    async def test_existing_source_stream_is_never_retargeted(self):
        await self.request('register', 'greeter', '/greeter')
        reader, writer = await self.connect()
        writer.write(b'one'); await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(11), 1), b'greeter:one')
        self.target = '/desktop'
        self.locked = True
        await self.request('register')
        writer.write(b'two'); await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(11), 1), b'greeter:two')
        self.assertEqual(self.received['desktop'], [])

    async def test_no_handover_without_authenticated_lease_or_for_foreign_uid(self):
        registration, _, _ = await self.request('register', 'greeter', '/greeter')
        self.target = '/desktop'
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(registration.readline(), .04)
        self.target = None
        source, _, reply = await self.request('lease', 'greeter', '/greeter')
        self.assertEqual(reply, b'GUARDED\n')
        self.target_uid = os.getuid() + 1
        self.target = '/desktop'
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(source.readline(), .04)
        reader, _ = await self.connect()
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
        self.assertTrue((self.directory / 'boot-guard').exists())

    async def test_wait_has_timeout_disconnect_and_session_change_bounds(self):
        self.target = '/desktop'
        self.gateway.handover_timeout = .06
        reader, _ = await self.connect()
        self.assertEqual(await asyncio.wait_for(reader.read(), .5), b'')
        self.gateway.handover_timeout = .3
        reader, writer = await self.connect()
        await self.until(lambda: self.gateway.waiting_relays == 1)
        writer.write(b'partial-handshake'); await writer.drain()
        writer.close(); await writer.wait_closed()
        await self.until(lambda: self.gateway.waiting_relays == 0)
        reader, _ = await self.connect()
        await self.until(lambda: self.gateway.waiting_relays == 1)
        self.target = '/replacement'
        self.assertEqual(await asyncio.wait_for(reader.read(), .5), b'')
        self.assertEqual(self.received['desktop'], [])

    async def test_waiting_clients_share_queries_and_are_capped_at_32(self):
        self.target = '/desktop'
        self.gateway.handover_timeout = 2
        self.gateway.target_cache_ttl = .5
        self.gateway.target_session = Mock(return_value='/desktop')
        peers = await asyncio.gather(*(self.connect() for _ in range(32)))
        await self.until(lambda: self.gateway.waiting_relays == 32)
        reader, _ = await self.connect()
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
        self.assertEqual(self.gateway.target_session.call_count, 1)
        for _, writer in peers:
            writer.close()
        await self.until(lambda: self.gateway.waiting_relays == 0)

    async def test_wait_buffer_is_bounded_and_never_reaches_an_unready_backend(self):
        self.target = '/desktop'
        reader, writer = await self.connect()
        writer.write(b'x' * 65537); await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
        self.assertEqual(self.received['desktop'], [])
        await self.until(lambda: self.gateway.waiting_relays == 0)

    async def test_receiver_failure_during_target_lookup_cannot_bypass_buffer_limit(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed_lookup():
            entered.set()
            await release.wait()
            return '/desktop'
        self.gateway.lookup_target = delayed_lookup
        self.gateway.endpoints['desktop'] = (1, '/desktop', 0)
        reader = asyncio.StreamReader()
        waiting = asyncio.create_task(self.gateway.wait_for_desktop('/desktop', reader))
        await entered.wait()
        reader.feed_data(b'x' * 65537)
        await asyncio.sleep(.01)
        release.set()
        with self.assertRaisesRegex(RuntimeError, 'buffer limit'):
            await waiting

    async def test_new_session_forces_refresh_of_production_negative_cache(self):
        self.gateway.target_cache_ttl = .5
        self.assertIsNone(await self.gateway.lookup_target())
        self.target = '/desktop'
        reader, writer = await self.connect()
        writer.write(b'new'); await writer.drain()
        await self.until(lambda: self.gateway.waiting_relays == 1)
        self.locked = True
        await self.request('register')
        self.assertEqual(await asyncio.wait_for(reader.readexactly(11), 1), b'desktop:new')

    async def test_closing_old_session_does_not_block_new_greeter(self):
        old = dict(self.properties('/desktop'), State='closing')
        self.gateway.user_session_exists = lambda: has_native_user_session(
            lambda: [('old', os.getuid(), 'irrelevant', 'seat0', '/old')], lambda path: old)
        await self.request('register', 'greeter', '/greeter')
        reader, writer = await self.connect()
        writer.write(b'new'); await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(11), 1), b'greeter:new')

    async def test_concurrent_authenticated_leases_keep_one_owner(self):
        await self.request('register', 'greeter', '/greeter')
        requests = await asyncio.gather(*(self.request('lease', 'greeter', '/greeter') for _ in range(2)))
        self.assertEqual(sum(reply == b'GUARDED\n' for _, _, reply in requests), 1)
        self.assertEqual(sum(b'error' in reply for _, _, reply in requests), 1)

    async def test_async_metadata_lookup_rechecks_newly_armed_guard(self):
        entered, release = threading.Event(), threading.Event()
        def delayed(path):
            entered.set()
            release.wait(1)
            return self.properties(path)
        self.gateway.session_info = delayed
        validation = asyncio.create_task(self.gateway.validate_async(os.getuid(), 'desktop', '/desktop'))
        await self.until(entered.is_set)
        self.gateway.arm_guard()
        release.set()
        with self.assertRaisesRegex(PermissionError, 'must be locked'):
            await validation

    async def test_pending_shutdown_and_restart_require_fresh_registration(self):
        self.gateway.arm_guard()
        self.target = '/desktop'
        reader, _ = await self.connect()
        await self.until(lambda: self.gateway.waiting_relays == 1)
        await asyncio.wait_for(self.gateway.shutdown(self.control, self.relay), 2)
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
        self.assertTrue((self.directory / 'boot-guard').exists())
        fresh, control, relay = await self.start_gateway()
        reader, writer = await self.connect(relay)
        writer.write(b'new'); await writer.drain()
        await self.until(lambda: fresh.waiting_relays == 1)
        self.locked = True
        await self.request('register')
        self.assertEqual(await asyncio.wait_for(reader.readexactly(11), 1), b'desktop:new')

    async def test_local_reclaim_requires_real_shell_status_not_request_booleans(self):
        self.gateway.arm_guard()
        self.auth_status['local-authenticated'] = False
        _, _, reply = await self.request('local-reclaim', **{'local-authenticated': True})
        self.assertIn(b'error', reply)
        self.assertTrue((self.directory / 'boot-guard').exists())
        self.auth_status['local-authenticated'] = True
        self.assertEqual((await self.request('local-reclaim'))[2], b'RECLAIMED\n')
        self.assertFalse((self.directory / 'boot-guard').exists())
        self.assertFalse(self.locked)
        self.assertEqual((await self.request('register'))[2], b'READY\n')

    async def test_local_reclaim_rejects_wrong_role_session_owner_or_failed_api(self):
        self.gateway.arm_guard()
        for changes in ({'state': 'remote'}, {'has-owner': True}, {'remote-inhibited': False},
                        {'session-path': '/different'}, {'local-authenticated': False}):
            with self.subTest(changes=changes):
                original = dict(self.auth_status)
                self.auth_status.update(changes)
                self.assertIn(b'error', (await self.request('local-reclaim'))[2])
                self.assertTrue((self.directory / 'boot-guard').exists())
                self.auth_status = original
        self.assertIn(b'error', (await self.request('local-reclaim', 'greeter', '/greeter'))[2])
        with self.assertRaises(PermissionError):
            await self.gateway.local_reclaim(os.getuid() + 1, 'desktop', '/desktop')
        self.gateway.native_status = Mock(side_effect=RuntimeError('bus unavailable'))
        reply = json.loads((await self.request('local-reclaim'))[2])
        self.assertTrue(reply['retryable'])
        self.assertTrue((self.directory / 'boot-guard').exists())
        self.gateway.native_status = Mock(side_effect=PermissionError('wrong Shell owner'))
        reply = json.loads((await self.request('local-reclaim'))[2])
        self.assertFalse(reply['retryable'])
        self.assertTrue((self.directory / 'boot-guard').exists())


class DiscoveryTests(unittest.TestCase):
    def test_only_unique_configured_physical_wayland_user_is_a_target(self):
        base = {'User': [1000, '/user'], 'Seat': ['seat0', '/seat'], 'Class': 'user',
                'Type': 'wayland', 'Remote': False, 'State': 'opening'}
        sessions = lambda: [('3', 1000, 'irrelevant-name', 'seat0', '/target')]
        self.assertEqual(find_native_desktop_session(1000, sessions, lambda path: base), '/target')
        for changes in ({'User': [2000, '/other']}, {'Remote': True}, {'Class': 'greeter'},
                        {'Type': 'x11'}, {'State': 'closing'}, {'Seat': ['seat1', '/seat']}):
            self.assertIsNone(find_native_desktop_session(1000, sessions, lambda path: dict(base, **changes)))
        both = lambda: sessions() + [('4', 1000, 'also-irrelevant', 'seat0', '/second')]
        self.assertIsNone(find_native_desktop_session(1000, both, lambda path: base))

    def test_root_status_query_drops_uid_uses_isolated_python_and_checks_shell_owner(self):
        result = subprocess.CompletedProcess([], 0, '{"state":"local"}', '')
        with patch('takeover.login_gateway.os.geteuid', return_value=0), \
             patch('takeover.login_gateway.pwd.getpwuid', return_value=SimpleNamespace(pw_gid=1000)), \
             patch('takeover.login_gateway.subprocess.run', return_value=result) as run:
            self.assertEqual(read_native_status(1000), {'state': 'local'})
        args, kwargs = run.call_args
        self.assertEqual(args[0], ['/usr/bin/python3', '-I', '-', '1000'])
        self.assertEqual((kwargs['user'], kwargs['group'], kwargs['extra_groups']), (1000, 1000, []))
        self.assertNotIn('shell', kwargs)
        self.assertIn('GetConnectionUnixProcessID', kwargs['input'])
        self.assertIn('org.gnome.Shell@ubuntu.service', kwargs['input'])


if __name__ == '__main__':
    unittest.main()
