"""System RDP entrypoint. Relay encrypted RDP; GDM performs account authentication.

Endpoints register over a root-owned Unix socket. Peer credentials and logind
must agree on the physical session. A root-owned boot guard is armed BEFORE
remote input reaches the greeter and survives disconnects until a locked native
session is ready. No passwords or desktop pixels are handled here.
"""
import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import pwd
import signal
import socket
import stat
import struct
import subprocess
import threading

from .greeter_identity import is_native_greeter_session, session_properties, logind_sessions

RUN = Path('/run/ubuntu-native-login')
CONFIG = Path('/etc/ubuntu-native-login/config.json')


def boot_guard_present(path=RUN / 'boot-guard'):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            return stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
        finally:
            os.close(fd)
    except FileNotFoundError:
        return False


def has_native_user_session(sessions=logind_sessions, info=session_properties):
    for _, _, _, seat, path in sessions():
        if seat != 'seat0':
            continue
        props = info(path)
        if (props.get('Class') in ('user', 'user-early') and props.get('Type') in ('wayland', 'x11') and
                props.get('Remote') is False and props.get('State') != 'closing'):
            return True
    return False


def native_desktop_properties(uid, props):
    return (props.get('User', [-1])[0] == uid and props.get('Seat', [''])[0] == 'seat0' and
            props.get('Type') == 'wayland' and props.get('Class') == 'user' and
            props.get('Remote') is False and props.get('State') != 'closing')


def find_native_desktop_session(uid, sessions=logind_sessions, info=session_properties):
    candidates = []
    for _, owner, _, seat, path in sessions():
        if owner == uid and seat == 'seat0' and native_desktop_properties(uid, info(path)):
            candidates.append(path)
    return candidates[0] if len(candidates) == 1 else None


def read_native_status(uid):
    """Read the actual Shell's status as its user, without trusting request data.

    A root EXTERNAL connection is not accepted by every user bus. Dropping to
    the configured UID for isolated Python avoids a PAM session or shell.
    Name ownership must match that user's running GNOME Shell systemd service.
    """
    script = '''import json,os,sys
from gi.repository import Gio,GLib
uid=int(sys.argv[1])
if os.getuid()!=uid: raise SystemExit(3)
bus=Gio.DBusConnection.new_for_address_sync('unix:path=/run/user/'+str(uid)+'/bus',Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT|Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,None,None)
def call(name,path,interface,method,args=None):
 return bus.call_sync(name,path,interface,method,args,None,0,1000,None).unpack()[0]
name='org.gnome.Mutter.NativeTakeover'
owner=call('org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus','GetConnectionUnixProcessID',GLib.Variant('(s)',(name,)))
owner_uid=call('org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus','GetConnectionUnixUser',GLib.Variant('(s)',(name,)))
unit=call('org.freedesktop.systemd1','/org/freedesktop/systemd1','org.freedesktop.systemd1.Manager','GetUnit',GLib.Variant('(s)',('org.gnome.Shell@ubuntu.service',)))
main_pid=call('org.freedesktop.systemd1',unit,'org.freedesktop.DBus.Properties','Get',GLib.Variant('(ss)',('org.freedesktop.systemd1.Service','MainPID')))
if owner_uid!=uid or not main_pid or owner!=main_pid: raise SystemExit(3)
status=call(name,'/org/gnome/Mutter/NativeTakeover',name,'GetStatus')
owner_after=call('org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus','GetConnectionUnixProcessID',GLib.Variant('(s)',(name,)))
if owner_after!=owner: raise SystemExit(3)
print(json.dumps(status))
'''
    command = ['/usr/bin/python3', '-I', '-', str(uid)]
    credentials = {}
    if os.geteuid() != uid:
        account = pwd.getpwuid(uid)
        credentials = {'user': uid, 'group': account.pw_gid, 'extra_groups': []}
    result = subprocess.run(command, input=script, capture_output=True, text=True, timeout=4,
                            **credentials)
    if result.returncode == 3:
        raise PermissionError('Native status owner is not the running GNOME Shell')
    if result.returncode:
        raise RuntimeError('Could not verify the running Shell local-authentication status')
    status = json.loads(result.stdout)
    if not isinstance(status, dict):
        raise RuntimeError('Invalid native authentication status')
    return status


class ClientDisconnected(Exception):
    pass


class RetryableControlError(RuntimeError):
    """No authorization was granted; a read-only prerequisite may recover."""


class Gateway:
    def __init__(self, directory, user_uid, session_info=session_properties,
                 user_session_exists=has_native_user_session,
                 greeter_identity=is_native_greeter_session, *, target_session=None,
                 native_status=read_native_status, handover_timeout=30, poll_interval=.2,
                 target_cache_ttl=.5):
        self.directory = Path(directory)
        self.user_uid = user_uid
        self.session_info = session_info
        self.user_session_exists = user_session_exists
        self.greeter_identity = greeter_identity
        self.target_session = target_session or (lambda: find_native_desktop_session(self.user_uid))
        self.native_status = native_status
        self.handover_timeout = handover_timeout
        self.poll_interval = poll_interval
        self.target_cache_ttl = target_cache_ttl
        self.target_cache = None
        self.target_cache_time = float('-inf')
        self.target_query = None
        self.query_tasks = set()
        self.query_slots = threading.BoundedSemaphore(8)
        self.handover_sent_to = None
        self.handover_tasks = set()
        self.waiting_relays = 0
        self.endpoints = {}
        self.greeter_lease = None
        self.sequence = 0
        self.client_tasks = set()
        self.client_writers = set()
        self.stopping = False

    def track_client(self, writer):
        if self.stopping:
            writer.close()
            return False
        task = asyncio.current_task()
        self.client_tasks.add(task)
        task.add_done_callback(self.client_tasks.discard)
        self.client_writers.add(writer)
        return True

    async def close_connection(self, writer):
        writer.close()
        try:
            # A peer which stopped reading must not hold systemd shutdown open.
            async with asyncio.timeout(.25):
                await writer.wait_closed()
        except asyncio.CancelledError:
            writer.transport.abort()
            raise
        except Exception:
            writer.transport.abort()
        finally:
            self.client_writers.discard(writer)

    async def shutdown(self, *servers, timeout=1.5):
        """Drop gateway-owned sockets before awaiting asyncio.Server closure.

        Since Python 3.12.1, Server.wait_closed also waits for active clients.
        Endpoint registrations intentionally remain open until the gateway
        closes them, so an async-with Server exit alone waits indefinitely.
        Closing this control socket lets the supervisor re-register while its
        compositor, broker and RDP daemon remain running. Never clear the guard.
        """
        self.stopping = True
        servers = tuple(server for server in servers if server is not None)
        deadline = asyncio.get_running_loop().time() + timeout
        for server in servers:
            server.close()  # Stop accepting before taking the client snapshot.
        for writer in tuple(self.client_writers):
            writer.close()
        for server in servers:
            # Also catches accepted transports whose handler has not run yet.
            if hasattr(server, 'close_clients'):
                server.close_clients()
        tasks = tuple(self.client_tasks | self.handover_tasks | self.query_tasks)
        for task in tasks:
            task.cancel()
        pending = set()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout / 2)
        for writer in tuple(self.client_writers):
            writer.transport.abort()
        for server in servers:
            if hasattr(server, 'abort_clients'):
                server.abort_clients()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=max(0, deadline - asyncio.get_running_loop().time()))
        for task in tasks:
            if task.done() and not task.cancelled():
                task.exception()  # Retrieve failures from already-finishing handlers.
        remaining = max(.001, deadline - asyncio.get_running_loop().time())
        try:
            async with asyncio.timeout(remaining):
                await asyncio.gather(*(server.wait_closed() for server in servers))
        except TimeoutError:
            # All transports have already been aborted; never wait for systemd's
            # ninety-second stop timeout just to observe a late close callback.
            print(json.dumps({'event': 'login-shutdown-close-timeout'}), flush=True)

    def validate(self, uid, role, session, *, require_boot_lock=True):
        if role not in ('greeter', 'desktop') or (role == 'desktop' and uid != self.user_uid):
            raise PermissionError('Endpoint role does not match Unix peer UID')
        props = self.session_info(session)
        if role == 'greeter' and not self.greeter_identity(uid, props):
            raise PermissionError('Endpoint is not a current native GDM greeter')
        if (props.get('User', [-1])[0] != uid or props.get('Seat', [''])[0] != 'seat0' or
                props.get('Type') != 'wayland' or props.get('Remote') is not False or
                props.get('State') == 'closing' or
                props.get('Class') != ('greeter' if role == 'greeter' else 'user')):
            raise PermissionError('Endpoint is not the expected native seat0 session')
        if require_boot_lock and role == 'desktop' and not props.get('LockedHint') and (self.directory / 'boot-guard').exists():
            raise PermissionError('New desktop must be locked before remote handover')
        return props

    async def validate_async(self, uid, role, session, *, require_boot_lock=True):
        if role not in ('greeter', 'desktop') or (role == 'desktop' and uid != self.user_uid):
            raise PermissionError('Endpoint role does not match Unix peer UID')
        props = await self.query(lambda: self.validate(uid, role, session, require_boot_lock=False))
        # Check the guard after awaiting metadata, on the same loop turn as the
        # registration mutation. A concurrent greeter may have armed it meanwhile.
        if require_boot_lock and role == 'desktop' and not props.get('LockedHint') and (self.directory / 'boot-guard').exists():
            raise PermissionError('New desktop must be locked before remote handover')
        return props

    async def query(self, callback, *args, timeout=2):
        """Bound synchronous discovery without an executor shutdown dependency.

        Gio synchronous calls have their own RPC deadlines, but cancellation of
        asyncio.to_thread does not stop its worker and asyncio.run joins that
        executor on exit. A capped set of daemon workers cannot pin shutdown or
        grow without bound if the system bus becomes unresponsive.
        """
        if not self.query_slots.acquire(blocking=False):
            raise RuntimeError('Native session query capacity is temporarily exhausted')
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        def work():
            value = error = None
            try:
                value = callback(*args)
            except Exception as caught:
                error = caught
            finally:
                self.query_slots.release()
            def deliver():
                if not future.done():
                    if error is not None:
                        future.set_exception(error)
                    else:
                        future.set_result(value)
            with contextlib.suppress(RuntimeError):  # Event loop may have shut down.
                loop.call_soon_threadsafe(deliver)
        threading.Thread(target=work, daemon=True, name='native-session-query').start()
        return await asyncio.wait_for(future, timeout)

    async def lookup_target(self, *, force=False):
        loop = asyncio.get_running_loop()
        if not force and loop.time() - self.target_cache_time < self.target_cache_ttl:
            return self.target_cache
        if self.target_query is None or self.target_query.done():
            async def discover():
                async with asyncio.timeout(2):
                    target = await self.query(self.target_session)
                    if target is not None:
                        props = await self.query(self.session_info, target)
                        if not native_desktop_properties(self.user_uid, props):
                            target = None
                self.target_cache = target
                self.target_cache_time = loop.time()
                return target
            self.target_query = asyncio.create_task(discover())
            self.query_tasks.add(self.target_query)
            def finished(task):
                self.query_tasks.discard(task)
                if not task.cancelled():
                    task.exception()
            self.target_query.add_done_callback(finished)
        return await asyncio.shield(self.target_query)

    async def send_handover(self, writer, target):
        if (writer is None or self.greeter_lease is not writer or
                self.handover_sent_to is writer or writer.is_closing()):
            return
        # Only an authenticated greeter broker has a lease. TCP clients never
        # receive control commands; GRD emits the RDP redirection after NLA.
        self.handover_sent_to = writer
        try:
            writer.write(b'HANDOVER\n')
            await asyncio.wait_for(writer.drain(), 1)
            print(json.dumps({'event': 'login-handover-requested', 'target_session': target}), flush=True)
        except (OSError, TimeoutError):
            print(json.dumps({'event': 'login-handover-source-unavailable'}), flush=True)

    async def watch_greeter_login(self, writer):
        failed = False
        while not self.stopping and self.greeter_lease is writer and not writer.is_closing():
            try:
                target = await self.lookup_target()
                failed = False
                if target is not None:
                    # GDM may kill its greeter before target RDP startup. Ask
                    # the source to redirect as soon as PAM/logind creates the
                    # configured physical user session, not at backend READY.
                    await self.send_handover(writer, target)
                    return
            except Exception as error:
                if not failed:
                    print(json.dumps({'event': 'login-handover-discovery-unavailable',
                                      'reason': str(error)}), flush=True)
                    failed = True
            await asyncio.sleep(self.poll_interval)

    async def local_reclaim(self, uid, role, session):
        if uid != self.user_uid or role != 'desktop':
            raise PermissionError('Only the configured desktop user may reclaim startup privacy')
        async def confirmed_session():
            try:
                return await self.validate_async(uid, role, session, require_boot_lock=False)
            except PermissionError:
                raise
            except Exception as error:
                raise RetryableControlError('Physical session metadata is temporarily unavailable') from error
        props = await confirmed_session()
        if props.get('Active') is not True or self.greeter_lease is not None or 'desktop' in self.endpoints:
            raise PermissionError('Physical session is inactive or another greeter controller exists')
        try:
            status = await self.query(self.native_status, uid, timeout=4.5)
        except PermissionError:
            raise
        except Exception as error:
            raise RetryableControlError('Native local-authentication status is temporarily unavailable') from error
        if (status.get('state') != 'local' or status.get('local-authenticated') is not True or
                status.get('has-owner') is not False or status.get('remote-inhibited') is not True or
                status.get('session-path') != session):
            raise PermissionError('The running Shell did not confirm authenticated local recovery')
        # Revalidate after the independent bus query; never use client-supplied
        # authentication flags, a different physical session, or greeter status.
        props = await confirmed_session()
        if props.get('Active') is not True or self.greeter_lease is not None or 'desktop' in self.endpoints:
            raise PermissionError('Physical session changed during local recovery')
        (self.directory / 'boot-guard').unlink(missing_ok=True)
        print(json.dumps({'event': 'login-local-reclaimed', 'session': session}), flush=True)

    async def wait_for_desktop(self, target, reader):
        if self.waiting_relays >= 32:
            raise RuntimeError('Too many connections waiting for native desktop startup')
        self.waiting_relays += 1
        buffered = bytearray()
        async def receive_while_waiting():
            while True:
                data = await reader.read(4096)
                if not data:
                    raise ClientDisconnected()
                buffered.extend(data)
                if len(buffered) > 65536:
                    raise RuntimeError('Client exceeded the startup handshake buffer limit')
        receiving = asyncio.create_task(receive_while_waiting())
        try:
            async with asyncio.timeout(self.handover_timeout):
                while not self.stopping:
                    if receiving.done():
                        await receiving
                    if await self.lookup_target() != target:
                        raise RuntimeError('The pending native desktop session disappeared or changed')
                    if receiving.done():
                        await receiving
                    endpoint = self.endpoints.get('desktop')
                    if endpoint and endpoint[1] == target:
                        return endpoint, buffered
                    await asyncio.sleep(self.poll_interval)
            raise RuntimeError('Gateway is stopping')
        except TimeoutError as error:
            raise RuntimeError('Native desktop did not become ready within the startup wait limit') from error
        finally:
            receiving.cancel()
            await asyncio.gather(receiving, return_exceptions=True)
            self.waiting_relays -= 1

    def arm_guard(self):
        # Retain after an interrupted login. A later native startup will lock
        # before lifting it; clearing on EOF could expose a starting desktop.
        path = self.directory / 'boot-guard'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, 'w') as f:
            f.write('remote-gdm-login\n')
            f.flush()
            os.fsync(f.fileno())

    def status(self, uid):
        if uid not in (0, self.user_uid):
            raise PermissionError('Gateway status is restricted to root and the configured desktop user')
        return {'version': 1, 'endpoints': sorted(self.endpoints),
                'greeter_lease': self.greeter_lease is not None,
                'boot_guard': boot_guard_present(self.directory / 'boot-guard')}

    async def control(self, reader, writer):
        if not self.track_client(writer):
            return
        registered = None
        lease = False
        watcher = None
        try:
            peer = writer.get_extra_info('socket')
            _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            request = json.loads(await asyncio.wait_for(reader.readline(), 3))
            command = request['command']
            if command == 'status':
                writer.write((json.dumps(self.status(uid)) + '\n').encode())
                await writer.drain()
                return
            role, session = request['role'], request['session']
            if command == 'local-reclaim':
                await self.local_reclaim(uid, role, session)
                writer.write(b'RECLAIMED\n')
                await writer.drain()
                return
            await self.validate_async(uid, role, session)
            if command == 'register':
                if role in self.endpoints:
                    raise RuntimeError('Endpoint already registered')
                port = 3394 if role == 'greeter' else 3393
                self.sequence += 1
                registered = (role, self.sequence)
                self.endpoints[role] = (self.sequence, session, port)
                writer.write(b'READY\n')
                await writer.drain()
                if role == 'desktop':
                    # The user supervisor registers only after the boot guard
                    # has produced a confirmed lock and its backend is listening.
                    (self.directory / 'boot-guard').unlink(missing_ok=True)
                    await self.send_handover(self.greeter_lease, session)
            elif command == 'lease' and role == 'greeter':
                if self.greeter_lease or 'desktop' in self.endpoints:
                    raise RuntimeError('Another login or desktop is already available')
                if await self.query(self.user_session_exists):
                    raise RuntimeError('Existing desktop must be taken over by its native backend, not unlocked through GDM')
                await self.validate_async(uid, role, session)
                if self.greeter_lease or 'desktop' in self.endpoints:
                    raise RuntimeError('Another login or desktop became available')
                self.arm_guard()
                self.greeter_lease = writer
                self.handover_sent_to = None
                lease = True
                writer.write(b'GUARDED\n')
                await writer.drain()
                watcher = asyncio.create_task(self.watch_greeter_login(writer))
                self.handover_tasks.add(watcher)
                watcher.add_done_callback(self.handover_tasks.discard)
            else:
                raise ValueError('Unsupported control command')
            await reader.read()  # Endpoint lifetime, never arbitrary commands.
        except Exception as error:
            with contextlib.suppress(Exception):
                writer.write((json.dumps({'error': str(error),
                                         'retryable': isinstance(error, RetryableControlError)}) + '\n').encode())
                await writer.drain()
            print(json.dumps({'event': 'login-control-refused', 'reason': str(error)}), flush=True)
        finally:
            if registered:
                role, sequence = registered
                if self.endpoints.get(role, (None,))[0] == sequence:
                    del self.endpoints[role]
            if lease and self.greeter_lease is writer:
                self.greeter_lease = None
            if watcher:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            await self.close_connection(writer)

    async def relay(self, reader, writer):
        if not self.track_client(writer):
            return
        backend = None
        tasks = []
        try:
            buffered = b''
            endpoint = self.endpoints.get('desktop')
            if not endpoint:
                target = await self.lookup_target()
                if target is not None:
                    endpoint, buffered = await self.wait_for_desktop(target, reader)
                elif await self.query(self.user_session_exists):
                    # PAM may have created the target just after a cached None.
                    # Refresh once before classifying this as a foreign session.
                    target = await self.lookup_target(force=True)
                    if target is None:
                        raise RuntimeError('Native desktop exists but its RDP backend is not ready')
                    endpoint, buffered = await self.wait_for_desktop(target, reader)
                else:
                    endpoint = self.endpoints.get('greeter')
            if not endpoint:
                raise RuntimeError('No native desktop or GDM greeter is ready')
            remote, backend = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', endpoint[2]), 3)
            self.client_writers.add(backend)
            if buffered:
                backend.write(buffered)
                await backend.drain()
            async def copy(source, target):
                while data := await source.read(65536):
                    target.write(data)
                    await target.drain()
            tasks = [asyncio.create_task(copy(reader, backend)), asyncio.create_task(copy(remote, writer))]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except ClientDisconnected:
            pass
        except Exception as error:
            print(json.dumps({'event': 'login-relay-error', 'reason': str(error)}), flush=True)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for connection in (backend, writer):
                if connection:
                    await self.close_connection(connection)


async def serve(gateway, path, *, host='0.0.0.0', port=3392):
    path = Path(path)
    # systemd owns the singleton; never replace a live listener.
    if path.exists():
        with socket.socket(socket.AF_UNIX) as probe:
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                path.unlink()
            else:
                raise RuntimeError('Login gateway already running')
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    control = tcp = None
    try:
        control = await asyncio.start_unix_server(gateway.control, path=str(path), limit=4096)
        path.chmod(0o666)  # Authorization uses SO_PEERCRED plus logind, not file secrecy.
        tcp = await asyncio.start_server(gateway.relay, host=host, port=port)
        print(f'Native login gateway listening on {tcp.sockets[0].getsockname()[1]}', flush=True)
        await stopping.wait()
    finally:
        try:
            await gateway.shutdown(control, tcp)
        finally:
            path.unlink(missing_ok=True)
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)


async def main():
    if os.geteuid() != 0:
        raise SystemExit('The login gateway must run as a system service')
    config = json.loads(CONFIG.read_text())
    RUN.mkdir(mode=0o755, exist_ok=True)
    gateway = Gateway(RUN, pwd.getpwnam(config['user']).pw_uid)
    await serve(gateway, RUN / 'control.sock')


if __name__ == '__main__':
    asyncio.run(main())
