#!/usr/bin/env python3
"""Own the local viewer and grant revocable leases to authenticated GRD peers.

The broker does not authenticate passwords. GNOME authenticates the physical
session; FreeRDP authenticates RDP before the patched daemon requests a lease.
All local IPC is private to the workspace's uid.
"""
import argparse
import asyncio
import ctypes
from dataclasses import dataclass, field
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import struct
import time


def parent_death_guard(parent_pid):
    # Called in the child immediately before exec. This also covers SIGKILL of
    # the broker, not just orderly shutdown or systemd control-group cleanup.
    if ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent_pid:
        os._exit(126)


def log(event, **fields):
    print(json.dumps({'time': time.time(), 'event': event, **fields}), flush=True)


async def command(*args, env=None):
    p = await asyncio.create_subprocess_exec(*args, env=env, stdout=asyncio.subprocess.PIPE,
                                            stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), 4)
    except BaseException:
        p.kill()
        await p.wait()
        raise
    if p.returncode:
        raise RuntimeError(err.decode().strip() or f'{args[0]} failed')
    return out.decode().strip()


class LocalDesktop:
    """The physical desktop is independent of the persistent working desktop."""
    def __init__(self, registry):
        self.registry = Path(registry)

    def config(self):
        st = self.registry.stat()
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise RuntimeError('local desktop registry must be owned by current uid, mode 0600')
        return json.loads(self.registry.read_text())

    async def session(self):
        cfg = self.config()
        sessions = json.loads(await command('loginctl', 'list-sessions', '--json=short'))
        ids = {str(s['session']) for s in sessions}
        props = None
        if cfg['session'] in ids:
            props = dict(line.split('=', 1) for line in (await command(
                'loginctl', 'show-session', cfg['session'], '-p', 'User', '-p', 'Seat',
                '-p', 'Type', '-p', 'State', '-p', 'LockedHint')).splitlines())
        if props is None or props.get('State') == 'closing':
            # Refuse when a new physical session exists but has not registered yet.
            for s in sessions:
                if s.get('seat') != 'seat0' or s.get('uid') != os.getuid():
                    continue
                p = dict(line.split('=', 1) for line in (await command(
                    'loginctl', 'show-session', str(s['session']), '-p', 'Type', '-p', 'State')).splitlines())
                if p.get('Type') in ('wayland', 'x11') and p.get('State') != 'closing':
                    raise RuntimeError('physical session changed; waiting for local registration')
            return None
        if props.get('User') != str(os.getuid()) or props.get('Seat') != 'seat0' or props.get('Type') != 'wayland':
            raise RuntimeError('registered session is not this user\'s physical Wayland desktop')
        return props

    async def screen_active(self):
        cfg = self.config()
        reply = await command('gdbus', 'call', '--address', cfg['bus'], '--dest', 'org.gnome.ScreenSaver',
                              '--object-path', '/org/gnome/ScreenSaver', '--method', 'org.gnome.ScreenSaver.GetActive')
        if reply not in ('(true,)', '(false,)'):
            raise RuntimeError('unexpected screen-lock response')
        return reply == '(true,)'

    async def locked(self):
        props = await self.session()
        if props is None:
            return True
        return props['LockedHint'] == 'yes' and await self.screen_active()

    async def available_unlocked(self):
        props = await self.session()
        return props is not None and props['LockedHint'] == 'no' and not await self.screen_active()

    async def lock(self):
        if await self.session() is None:
            return
        if await self.locked():
            return
        cfg = self.config()
        # Some ScreenSaver versions await a transition signal even when another
        # caller has already locked. The observed lock state is the authority.
        call = asyncio.create_task(command('gdbus', 'call', '--address', cfg['bus'],
            '--dest', 'org.gnome.ScreenSaver', '--object-path', '/org/gnome/ScreenSaver',
            '--method', 'org.gnome.ScreenSaver.Lock'))
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if await self.locked():
                    log('physical_locked')
                    return
                await asyncio.sleep(.1)
            raise RuntimeError('physical lock not confirmed')
        finally:
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)

    def viewer_env(self):
        cfg = self.config()
        env = dict(os.environ)
        for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY'):
            env.pop(key, None)
            if cfg.get(key):
                env[key] = cfg[key]
        env['GDK_BACKEND'] = 'wayland'
        return env


@dataclass
class Lease:
    writer: asyncio.StreamWriter
    done: asyncio.Event = field(default_factory=asyncio.Event)


class Broker:
    def __init__(self, desktop, viewer_command, status_path=None):
        self.desktop = desktop
        self.viewer_command = viewer_command
        self.status_path = status_path
        self.mutex = asyncio.Lock()
        self.remote = None
        self.viewer = None
        self.auto_local = False
        self.closing = False
        self.local_paused = False
        self.viewer_failures = 0
        self.viewer_retry_at = 0

    def status(self):
        return {'remote': self.remote is not None,
                'viewer_pid': self.viewer.pid if self.viewer and self.viewer.returncode is None else None,
                'auto_local': self.auto_local,
                'local_paused': self.local_paused,
                'viewer_failures': self.viewer_failures}

    def publish(self):
        log('state', **self.status())
        if self.status_path:
            p = Path(self.status_path)
            tmp = p.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.status()) + '\n')
            os.chmod(tmp, 0o600)
            tmp.replace(p)

    async def stop_viewer(self):
        if self.viewer and self.viewer.returncode is None:
            self.viewer.terminate()
            try:
                await asyncio.wait_for(self.viewer.wait(), 2)
            except asyncio.TimeoutError:
                self.viewer.kill()
                await asyncio.wait_for(self.viewer.wait(), 2)
            log('local_viewer_stopped')
        self.viewer = None

    async def revoke_remote(self):
        lease = self.remote
        if not lease:
            return
        lease.writer.write(b'REVOKE\n')
        try:
            await lease.writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        # EOF from patched GRD acknowledges that its Mutter session and EI are stopped.
        await asyncio.wait_for(lease.done.wait(), 7)
        if self.remote is lease:
            self.remote = None
        log('remote_revocation_confirmed')

    async def local(self, manual=True):
        async with self.mutex:
            if not await self.desktop.available_unlocked():
                raise RuntimeError('unlock the physical desktop with your normal GNOME login first')
            await self.revoke_remote()
            # Authentication/lock state might have changed while waiting for GRD.
            if not await self.desktop.available_unlocked():
                raise RuntimeError('physical desktop locked during takeover')
            self.local_paused = False
            if manual:
                self.viewer_failures = 0
                self.viewer_retry_at = 0
            if not self.viewer or self.viewer.returncode is not None:
                self.viewer = await asyncio.create_subprocess_exec(*self.viewer_command,
                    env=self.desktop.viewer_env(), stdin=asyncio.subprocess.DEVNULL,
                    preexec_fn=lambda parent=os.getpid(): parent_death_guard(parent))
            self.auto_local = True
            self.publish()

    async def acquire_remote(self, reader, writer):
        lease = Lease(writer)
        try:
            async with self.mutex:
                if self.remote or self.closing:
                    raise RuntimeError('another remote client already owns the desktop')
                await self.desktop.lock()
                await self.stop_viewer()
                if not await self.desktop.locked():
                    raise RuntimeError('physical lock changed before grant')
                self.remote = lease
                self.local_paused = False
                self.viewer_failures = 0
                self.viewer_retry_at = 0
                self.auto_local = True
                writer.write(b'GRANTED\n')
                await writer.drain()
                log('remote_granted')
                self.publish()
            # The client sends no further payload. EOF means its lease has ended.
            await reader.read(1)
        finally:
            lease.done.set()  # Set before mutex: local() may be awaiting this event.
            async with self.mutex:
                if self.remote is lease:
                    self.remote = None
                    log('remote_released')
                    self.publish()

    async def handle(self, reader, writer):
        try:
            peer = writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, uid, _ = struct.unpack('3i', peer)
            if uid != os.getuid():
                raise RuntimeError('wrong uid')
            request = await asyncio.wait_for(reader.readline(), 3)
            if request == b'REMOTE\n':
                await self.acquire_remote(reader, writer)
            elif request == b'LOCAL\n':
                await self.local()
                writer.write(b'OK\n')
                await writer.drain()
            elif request == b'STATUS\n':
                writer.write(json.dumps(self.status()).encode() + b'\n')
                await writer.drain()
            else:
                raise RuntimeError('unknown request')
        except Exception as error:
            log('request_denied', reason=str(error))
            try:
                writer.write(b'DENIED!\n')
                await writer.drain()
            except ConnectionError:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def monitor_once(self):
        if not self.auto_local:
            return
        if self.viewer and self.viewer.returncode is not None:
            code = self.viewer.returncode
            self.viewer = None
            if code == 0:
                # Closing the local window or choosing Return must be respected.
                self.local_paused = True
            else:
                self.viewer_failures += 1
                self.viewer_retry_at = time.monotonic() + min(30, 2 ** min(self.viewer_failures, 5))
            log('local_viewer_exited', returncode=code, retry=code != 0)
            self.publish()
        unlocked = await self.desktop.available_unlocked()
        if not unlocked:
            # A subsequent normal password unlock should reopen the desktop,
            # even if the user previously closed its local window.
            self.local_paused = False
            if self.viewer:
                async with self.mutex:
                    await self.stop_viewer()
                    self.publish()
        elif self.remote:
            await self.local(manual=False)
        elif self.viewer is None and not self.local_paused and time.monotonic() >= self.viewer_retry_at:
            await self.local(manual=False)

    async def monitor(self):
        while not self.closing:
            try:
                await self.monitor_once()
            except Exception as error:
                # Unknown physical state must revoke both access paths.
                log('physical_state_error', reason=str(error))
                async with self.mutex:
                    await self.stop_viewer()
                    try:
                        await self.revoke_remote()
                    except Exception:
                        pass
            await asyncio.sleep(.3)


async def main(args):
    path = Path(args.socket)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_uid != os.getuid() or path.parent.stat().st_mode & 0o077:
        raise RuntimeError('socket directory must be private to current user')
    lock = open(path.with_suffix('.lock'), 'w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if path.exists():
        path.unlink()
    os.umask(0o077)
    broker = Broker(LocalDesktop(args.local_registry), args.viewer_command, args.status)
    broker.auto_local = True
    server = await asyncio.start_unix_server(broker.handle, path=str(path), limit=32)
    os.chmod(path, 0o600)
    broker.publish()
    monitor = asyncio.create_task(broker.monitor())
    quit_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, quit_event.set)
    try:
        async with server:
            await quit_event.wait()
    finally:
        broker.closing = True
        monitor.cancel()
        await broker.stop_viewer()
        if broker.remote:
            broker.remote.writer.close()
        path.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', required=True)
    parser.add_argument('--local-registry', required=True)
    parser.add_argument('--status')
    parser.add_argument('viewer_command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.viewer_command[:1] == ['--']:
        args.viewer_command.pop(0)
    if not args.viewer_command:
        parser.error('viewer command is required')
    asyncio.run(main(args))
