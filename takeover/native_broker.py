#!/usr/bin/env python3
"""Authenticated GRD gate for the ORIGINAL native GNOME session.

No compositor, local viewer, RDP loopback, or second desktop is launched here.
Mutter owns the privacy barrier and fails closed if this process disappears.
"""
import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import socket
import struct

from gi.repository import Gio, GLib
from takeover.monitor_layout import MonitorLayout
from takeover.remote_settings import save_scale, validate_scale

NAME = 'org.gnome.Mutter.NativeTakeover'
OBJECT = '/org/gnome/Mutter/NativeTakeover'


def resolution_request(line, verb):
    fields = line.split()
    if len(fields) != 3 or fields[0] != verb:
        raise ValueError('Invalid resolution request')
    width, height = map(int, fields[1:])
    if not 200 <= width <= 8192 or not 200 <= height <= 8192:
        raise ValueError('Resolution outside 200..8192')
    return width, height


class NativeDesktop:
    greeter = False

    def __init__(self, address, greeter=False):
        self.greeter = greeter
        self.connection = Gio.DBusConnection.new_for_address_sync(
            address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
            Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        self.connection.set_exit_on_close(False)
        self.revoked = asyncio.Event()
        self.connection.signal_subscribe(NAME, NAME, 'Revoked', OBJECT, None, 0,
                                          lambda *args: self.revoked.set())
        self.connection.connect('closed', lambda *args: self.revoked.set())

    async def call(self, method, params=None, timeout=7000):
        return await self.rpc(self.connection, NAME, OBJECT, NAME, method, params, timeout)

    async def rpc(self, bus, name, path, interface, method, params=None, timeout=3000):
        future = asyncio.get_running_loop().create_future()

        def done(connection, result):
            try:
                value = connection.call_finish(result).unpack()
            except GLib.Error as exc:
                if not future.done():
                    future.set_exception(RuntimeError(str(exc)))
            else:
                if not future.done():
                    future.set_result(value)
        bus.call(name, path, interface, method, params, None,
                             Gio.DBusCallFlags.NONE, timeout, None, done)
        return await future

    async def begin(self, session):
        self.revoked.clear()
        display = 'org.gnome.Mutter.DisplayConfig'
        properties = 'org.freedesktop.DBus.Properties'
        mode = (await self.rpc(self.connection, display, '/org/gnome/Mutter/DisplayConfig',
            properties, 'Get', GLib.Variant('(ss)', (display, 'PowerSaveMode'))))[0]
        if mode > 0:
            # Waking an unlocked desktop before Begin would expose it locally.
            # Require both lock indications, then activate the existing native
            # black/input barrier BEFORE requesting display power on.
            if not self.greeter:
                await self.confirm_lock(session)
        begin = asyncio.create_task(self.call('Begin', GLib.Variant('(o)', (session,))))
        try:
            if mode > 0:
                async with asyncio.timeout(3):
                    while not begin.done():
                        if self.revoked.is_set():
                            raise RuntimeError('Handover revoked before display wake')
                        if await self.state() in ('blanking', 'remote'):
                            break
                        await asyncio.sleep(.02)
                    if begin.done():
                        await begin  # Propagate refusal; never wake after failed Begin.
                    if self.revoked.is_set() or await self.state() not in ('blanking', 'remote'):
                        raise RuntimeError('Privacy barrier unavailable before display wake')
                    await self.rpc(self.connection, display, '/org/gnome/Mutter/DisplayConfig',
                        properties, 'Set', GLib.Variant('(ssv)',
                            (display, 'PowerSaveMode', GLib.Variant('i', 0))))
                    print(json.dumps({'event': 'native-display-wake', 'previous_power_mode': mode,
                                      'locked_before_wake': True, 'privacy_barrier_active': True}), flush=True)
            await begin
        finally:
            if not begin.done():
                begin.cancel()
            await asyncio.gather(begin, return_exceptions=True)
        if self.revoked.is_set() or await self.state() != 'remote':
            raise RuntimeError('Native handover revoked before GRD grant')

    async def confirm_lock(self, session):
        saver = 'org.gnome.ScreenSaver'
        system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        async def confirmed():
            active = (await self.rpc(self.connection, saver, '/org/gnome/ScreenSaver',
                                     saver, 'GetActive'))[0]
            locked = (await self.rpc(system, 'org.freedesktop.login1', session,
                'org.freedesktop.DBus.Properties', 'Get', GLib.Variant('(ss)',
                    ('org.freedesktop.login1.Session', 'LockedHint'))))[0]
            return active and locked
        if await confirmed():
            return
        # Lock's reply may wait for a frame while DPMS is off. Observe the
        # actual lock state independently instead of waiting for that reply.
        request = asyncio.create_task(self.rpc(self.connection, saver,
            '/org/gnome/ScreenSaver', saver, 'Lock', timeout=5000))
        try:
            async with asyncio.timeout(3):
                while not await confirmed():
                    if request.done():
                        await request
                    await asyncio.sleep(.05)
        finally:
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)

    async def end(self):
        await self.call('End')

    async def state(self):
        return (await self.call('GetState', timeout=2000))[0]


async def pump_glib():
    context = GLib.MainContext.default()
    while True:
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        await asyncio.sleep(.01)


class Broker:
    def __init__(self, desktop, session, layout, greeter=False, gateway=None):
        self.greeter = greeter
        self.gateway = gateway
        self.desktop = desktop
        self.session = session
        self.busy = False
        self.layout = layout

    async def read_updates(self, reader):
        while line := await reader.readline():
            size = resolution_request(line, b'RESIZE')
            # Coalesce resize bursts while the user drags the client window.
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), .15)
                except asyncio.TimeoutError:
                    break
                if not line:
                    return
                size = resolution_request(line, b'RESIZE')
            if not self.desktop.revoked.is_set() and await self.desktop.state() == 'remote':
                try:
                    self.layout.resize(*size)
                except Exception as exc:
                    print(json.dumps({'event': 'native-resize-rejected', 'reason': str(exc)}), flush=True)

    async def handle(self, reader, writer):
        owned = False
        login_reader = login_writer = None
        try:
            peer = writer.get_extra_info('socket')
            _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                raise RuntimeError('Wrong Unix peer uid')
            request = await asyncio.wait_for(reader.readline(), 2)
            if request.startswith(b'SCALE '):
                try:
                    percent = validate_scale(int(request.split()[1]))
                    if len(request.split()) != 2:
                        raise ValueError('Expected SCALE percent')
                    self.layout.set_scale(percent)
                    save_scale(percent)
                    response = {'ok': True, 'scale_percent': percent}
                except Exception as exc:
                    response = {'ok': False, 'error': str(exc)}
                writer.write((json.dumps(response) + '\n').encode())
                await writer.drain()
                return
            if request == b'STATUS\n':
                writer.write((json.dumps({'native_state': await self.desktop.state(),
                                          'remote': self.busy, 'session': self.session}) + '\n').encode())
                await writer.drain()
                return
            if (request != b'REMOTE\n' and not request.startswith(b'REMOTE ')) or self.busy:
                writer.write(b'DENIED!\n')
                await writer.drain()
                return
            size = resolution_request(request, b'REMOTE') if request != b'REMOTE\n' else None
            self.busy = owned = True  # no await between check and reservation
            if self.greeter:
                login_reader, login_writer = await asyncio.open_unix_connection(self.gateway)
                login_writer.write((json.dumps({'command':'lease','role':'greeter',
                                                'session':self.session}) + '\n').encode())
                await login_writer.drain()
                if await asyncio.wait_for(login_reader.readline(), 3) != b'GUARDED\n':
                    raise RuntimeError('System login guard refused remote greeter input')
            await self.desktop.begin(self.session)
            self.layout.enter()
            if size:
                try:
                    self.layout.resize(*size)
                except Exception as exc:
                    # Resolution preference must not turn a usable native
                    # session into a failed login. Keep the single-screen mode.
                    print(json.dumps({'event': 'native-resize-rejected', 'reason': str(exc)}), flush=True)
            if self.desktop.revoked.is_set() or await self.desktop.state() != 'remote':
                raise RuntimeError('Native handover revoked during monitor switch')
            writer.write(b'GRANTED\n')
            await writer.drain()
            eof = asyncio.create_task(self.read_updates(reader))
            revoke = asyncio.create_task(self.desktop.revoked.wait())
            handover = asyncio.create_task(login_reader.readline()) if login_reader else None
            try:
                done, _ = await asyncio.wait([eof, revoke] + ([handover] if handover else []), return_when=asyncio.FIRST_COMPLETED)
                if eof in done:
                    await eof
                if handover and handover in done:
                    if await handover != b'HANDOVER\n':
                        raise RuntimeError('System login gateway disappeared')
                    writer.write(b'REDIRECT\n')
                    await writer.drain()
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(asyncio.shield(eof), 8)
                if revoke in done:
                    writer.write(b'REVOKE\n')
                    await writer.drain()
                    # GRD closes the lease only after its Mutter input/capture stops.
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(asyncio.shield(eof), 4)
            finally:
                eof.cancel()
                revoke.cancel()
                if handover:
                    handover.cancel()
                await asyncio.gather(eof, revoke, *([handover] if handover else []), return_exceptions=True)
        except (Exception, asyncio.CancelledError) as exc:
            print(json.dumps({'event': 'native-gate-refused', 'reason': str(exc)}), flush=True)
        finally:
            if login_writer:
                login_writer.close()
                with contextlib.suppress(Exception):
                    await login_writer.wait_closed()
            if owned:
                try:
                    self.layout.restore()
                except Exception as exc:
                    print(json.dumps({'event': 'native-monitor-restore-pending', 'reason': str(exc)}), flush=True)
                # Covers failed Begin/timeouts as well as healthy disconnects.
                with contextlib.suppress(Exception):
                    await self.desktop.end()
                self.busy = False
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def main(args):
    os.umask(0o077)
    path = Path(args.socket)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        raise RuntimeError('Socket already exists; refusing to replace another broker')
    desktop = NativeDesktop(args.bus, greeter=args.greeter)
    pump = asyncio.create_task(pump_glib())
    stopping = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stopping.set)
    server = None
    try:
        if await desktop.state() not in ('local', 'ready-guarded'):
            raise RuntimeError('Patched native Mutter is unavailable or not ready')
        layout = MonitorLayout(desktop.connection, path.parent / 'monitor-layout.json')
        layout.restore()
        broker = Broker(desktop, args.session, layout, greeter=args.greeter, gateway=args.gateway)
        server = await asyncio.start_unix_server(broker.handle, path=str(path), limit=256)
        os.chmod(path, 0o600)
        print(json.dumps({'event': 'native-ready', 'session': args.session}), flush=True)
        await stopping.wait()
    finally:
        if server:
            server.close()
            await server.wait_closed()
            path.unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            await desktop.end()
        desktop.connection.close_sync(None)  # Mutter also handles unexpected owner loss.
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', required=True)
    parser.add_argument('--session', required=True, help='logind physical session object path')
    parser.add_argument('--greeter', action='store_true')
    parser.add_argument('--gateway', default='/run/ubuntu-native-login/control.sock')
    parser.add_argument('--bus', default=os.environ.get('DBUS_SESSION_BUS_ADDRESS'))
    asyncio.run(main(parser.parse_args()))
