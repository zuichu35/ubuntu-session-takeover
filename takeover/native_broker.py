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
from takeover.remote_settings import client_scale, save_scale, validate_scale

NAME = 'org.gnome.Mutter.NativeTakeover'
OBJECT = '/org/gnome/Mutter/NativeTakeover'


# GRD allows four client monitors: the viewport plus three remote-only ones.
MAX_EXTRA_MONITORS = 3


def _size(width, height):
    if not 200 <= width <= 8192 or not 200 <= height <= 8192:
        raise ValueError('Resolution outside 200..8192')
    return width, height


def _scale_field(field):
    return field[:1] in (b'S', 'S') and field[1:].isdigit()


def requested_scale(line):
    """Scale the RDP client announced in a trailing 'S<percent>', else None."""
    fields = line.split()
    if len(fields) < 2 or not _scale_field(fields[-1]):
        return None
    return client_scale(int(fields[-1][1:]))


def layout_request(line, verbs):
    """Parse 'VERB w h [x y w h]... [S<percent>]': viewport, extra monitors.

    Extra positions are client pixels relative to the viewport's top-left.
    The optional client scale is read with requested_scale().
    """
    fields = line.split()
    if len(fields) > 1 and _scale_field(fields[-1]):
        fields.pop()
    if (not fields or fields[0] not in verbs or len(fields) < 3 or
            (len(fields) - 3) % 4):
        raise ValueError('Invalid layout request')
    values = [int(value) for value in fields[1:]]
    size = _size(*values[:2])
    extras = tuple(tuple(values[i:i + 4]) for i in range(2, len(values), 4))
    if len(extras) > MAX_EXTRA_MONITORS:
        raise ValueError('Too many remote monitors')
    for x, y, width, height in extras:
        _size(width, height)
        if abs(x) > 65536 or abs(y) > 65536:
            raise ValueError('Remote monitor position out of range')
    return fields[0], size, extras


# The scale announced by the RDP client always wins. Without one the desktop
# uses the user's saved setting. The GDM greeter runs as a dynamic gdm account
# and cannot read that file, so its fallback is a 1:1 login form.
GREETER_SCALE = 100


def initial_scale(greeter):
    """Fallback scale of a new broker; None reads the user's saved setting."""
    return GREETER_SCALE if greeter else None


def resolution_request(line, verb):
    _, size, extras = layout_request(line, (verb,))
    if extras:
        raise ValueError('Invalid resolution request')
    return size


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
        # Last scale the client announced in this lease. Only a change is
        # applied, so a manual scale survives resizing the client window.
        self.client_scale = None

    async def read_updates(self, reader):
        verbs = (b'RESIZE', b'LAYOUT')

        def parse(line):
            try:
                return layout_request(line, verbs) + (requested_scale(line),)
            except ValueError as exc:
                # A rejected layout keeps the current one and the lease.
                print(json.dumps({'event': 'native-resize-rejected', 'reason': str(exc)}), flush=True)
                return None
        while line := await reader.readline():
            request = parse(line)
            # Coalesce resize bursts while the user drags the client window.
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), .15)
                except asyncio.TimeoutError:
                    break
                if not line:
                    return
                request = parse(line) or request
            if request is None:
                continue
            verb, size, extras, scale = request
            if not self.desktop.revoked.is_set() and await self.desktop.state() == 'remote':
                options = {}
                if verb == b'LAYOUT':
                    options['extras'] = extras
                if scale is not None and scale != self.client_scale:
                    options['scale_percent'] = self.client_scale = scale
                try:
                    self.layout.resize(*size, **options)
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
                status = {'native_state': await self.desktop.state(),
                          'remote': self.busy, 'session': self.session}
                try:
                    # Dashboard facts; never a reason to fail the health check.
                    status.update(dict(self.layout.describe()),
                                  client_scale=self.client_scale if self.busy else None)
                    json.dumps(status)
                except Exception:
                    status = {key: status[key] for key in ('native_state', 'remote', 'session')}
                writer.write((json.dumps(status) + '\n').encode())
                await writer.drain()
                return
            if (request != b'REMOTE\n' and not request.startswith(b'REMOTE ')) or self.busy:
                writer.write(b'DENIED!\n')
                await writer.drain()
                return
            size, extras = None, ()
            if request != b'REMOTE\n':
                _, size, extras = layout_request(request, (b'REMOTE',))
            self.busy = owned = True  # no await between check and reservation
            self.client_scale = requested_scale(request)
            self.layout.begin_scale(self.client_scale)
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
                    self.layout.resize(*size, extras=extras,
                                       scale_percent=self.layout.scale_percent)
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
        layout = MonitorLayout(desktop.connection, path.parent / 'monitor-layout.json',
                               scale_percent=initial_scale(args.greeter))
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
