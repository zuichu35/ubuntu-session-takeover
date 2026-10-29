import asyncio
import contextlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from takeover.login_gateway import Gateway, boot_guard_present
from takeover.greeter_identity import is_native_greeter_session


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.locked = True
        def properties(path):
            if path not in ('/greeter', '/desktop'):
                raise ValueError('Unknown logind session')
            return {'User': [os.getuid(), '/user'], 'Seat':['seat0','/seat'], 'Type':'wayland',
                    'Remote':False, 'Class':'greeter' if path == '/greeter' else 'user',
                    'LockedHint':self.locked}
        self.gateway = Gateway(self.directory, os.getuid(), properties, lambda: False,
            lambda uid, props: is_native_greeter_session(uid, props, lambda value: value == os.getuid()),
            target_session=lambda: None)
        self.server = await asyncio.start_unix_server(self.gateway.control, path=str(self.directory/'control'))
        self.clients = []

    async def asyncTearDown(self):
        for _, writer in self.clients:
            writer.close()
            with contextlib.suppress(Exception): await writer.wait_closed()
        self.server.close()
        await self.server.wait_closed()
        await asyncio.sleep(.01)
        self.temp.cleanup()

    async def request(self, command, role, session):
        reader, writer = await asyncio.open_unix_connection(str(self.directory/'control'))
        self.clients.append((reader,writer))
        writer.write((json.dumps({'command':command,'role':role,'session':session})+'\n').encode())
        await writer.drain()
        return reader, writer, await asyncio.wait_for(reader.readline(),2)

    async def test_authenticated_greeter_handover_requires_locked_native_desktop(self):
        _,_,reply = await self.request('register','greeter','/greeter')
        self.assertEqual(reply,b'READY\n')
        reader,_,reply = await self.request('lease','greeter','/greeter')
        self.assertEqual(reply,b'GUARDED\n')
        self.assertTrue((self.directory/'boot-guard').exists())
        self.locked = False
        _,_,reply = await self.request('register','desktop','/desktop')
        self.assertIn(b'must be locked',reply)
        self.assertNotIn('desktop',self.gateway.endpoints)
        self.locked = True
        _,_,reply = await self.request('register','desktop','/desktop')
        self.assertEqual(reply,b'READY\n')
        self.assertEqual(await asyncio.wait_for(reader.readline(),2),b'HANDOVER\n')
        self.assertFalse((self.directory/'boot-guard').exists())

    async def test_disconnect_during_login_keeps_startup_guard(self):
        _,writer,reply = await self.request('lease','greeter','/greeter')
        self.assertEqual(reply,b'GUARDED\n')
        writer.close();await writer.wait_closed();await asyncio.sleep(.03)
        self.assertTrue((self.directory/'boot-guard').exists())
        self.locked=False
        _,_,reply=await self.request('register','desktop','/desktop')
        self.assertIn(b'must be locked',reply)

    async def test_existing_session_cannot_be_unlocked_through_gdm(self):
        self.gateway.user_session_exists = lambda: True
        _,_,reply=await self.request('lease','greeter','/greeter')
        self.assertIn(b'not unlocked through GDM',reply)
        self.assertFalse((self.directory/'boot-guard').exists())

    async def test_wrong_role_or_session_cannot_arm_guard(self):
        for role,session in [('desktop','/greeter'),('greeter','/desktop'),('greeter','/invalid')]:
            _,_,reply=await self.request('lease',role,session)
            self.assertIn(b'error',reply)
        self.assertFalse((self.directory/'boot-guard').exists())
        with self.assertRaises(PermissionError):
            self.gateway.validate(os.getuid()+1,'desktop','/desktop')

    async def test_duplicate_endpoint_or_login_does_not_replace_owner(self):
        _,_,reply=await self.request('register','greeter','/greeter')
        self.assertEqual(reply,b'READY\n')
        original=self.gateway.endpoints['greeter']
        _,_,reply=await self.request('register','greeter','/greeter')
        self.assertIn(b'already registered',reply)
        self.assertEqual(self.gateway.endpoints['greeter'],original)
        await self.request('lease','greeter','/greeter')
        _,_,reply=await self.request('lease','greeter','/greeter')
        self.assertIn(b'already available',reply)

    async def test_guard_marker_rejects_symlink_and_user_writable_file(self):
        path=self.directory/'marker'
        self.assertFalse(boot_guard_present(path))
        path.write_text('guard');path.chmod(0o666)
        self.assertFalse(boot_guard_present(path))
        link=self.directory/'link';link.symlink_to(path)
        with self.assertRaises(OSError): boot_guard_present(link)

    async def test_status_has_no_role_requirement_and_does_not_register_or_arm_guard(self):
        reader, writer = await asyncio.open_unix_connection(str(self.directory / 'control'))
        self.clients.append((reader, writer))
        writer.write(b'{"command":"status"}\n')
        await writer.drain()
        reply = json.loads(await asyncio.wait_for(reader.readline(), 2))
        self.assertEqual(reply, {'version': 1, 'endpoints': [],
                                 'greeter_lease': False, 'boot_guard': False})
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
        self.assertFalse((self.directory / 'boot-guard').exists())
        self.assertEqual(self.gateway.endpoints, {})

    async def test_status_rejects_unrelated_uid_and_returns_only_bounded_metadata(self):
        with self.assertRaises(PermissionError):
            self.gateway.status(os.getuid() + 1)
        await self.request('register', 'desktop', '/desktop')
        self.assertEqual(self.gateway.status(0), {'version': 1, 'endpoints': ['desktop'],
                                                'greeter_lease': False, 'boot_guard': False})

    async def test_greeter_identity_is_rechecked_but_desktop_uid_remains_fixed(self):
        dynamic_uid = os.getuid() + 70000
        properties = {'User': [dynamic_uid, '/dynamic'], 'Seat': ['seat0', '/seat'],
                      'Class': 'greeter', 'Type': 'wayland', 'Remote': False}
        accepted = True
        gateway = Gateway(self.directory, os.getuid(), lambda path: properties,
                          lambda: False, lambda uid, props: accepted and uid == dynamic_uid)
        gateway.validate(dynamic_uid, 'greeter', '/dynamic')
        accepted = False  # The dynamic account has expired; its UID is no longer trusted.
        with self.assertRaises(PermissionError):
            gateway.validate(dynamic_uid, 'greeter', '/dynamic')
        properties['Class'] = 'user'
        with self.assertRaises(PermissionError):
            gateway.validate(dynamic_uid, 'desktop', '/dynamic')
