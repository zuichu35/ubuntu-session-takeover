#!/usr/bin/env python3
"""Supervise one persistent GNOME workspace, independent of the physical login."""
import asyncio
import json
import os
import signal
import socket
import sys

from takeover.common import BUS, RUNTIME, private_json, workspace_env
from takeover.input_method import prepare as input_method_env


async def main():
    os.umask(0o077)
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    (RUNTIME / 'desktop').mkdir(mode=0o700, exist_ok=True)
    children = []
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)

    async def spawn(*args, child_env=None):
        p = await asyncio.create_subprocess_exec(*args, env=child_env or workspace_env())
        children.append(p)
        return p

    try:
        (RUNTIME / 'activation-env.json').unlink(missing_ok=True)
        (RUNTIME / 'workspace-bus').unlink(missing_ok=True)
        bus = await spawn('dbus-daemon', '--session', '--nofork', '--nopidfile', '--address=' + BUS)
        for _ in range(100):
            if (RUNTIME / 'workspace-bus').exists():
                break
            if bus.returncode is not None:
                raise RuntimeError('workspace bus failed')
            await asyncio.sleep(.1)
        shell = await spawn('/usr/bin/gnome-shell', '--headless', '--virtual-monitor=1920x1080',
                            '--wayland-display=wayland-takeover')
        for _ in range(300):
            p = await asyncio.create_subprocess_exec('gdbus', 'call', '--address', BUS,
                '--dest', 'org.gnome.Mutter.DisplayConfig', '--object-path', '/org/gnome/Mutter/DisplayConfig',
                '--method', 'org.gnome.Mutter.DisplayConfig.GetCurrentState',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            if await p.wait() == 0:
                break
            if shell.returncode is not None:
                raise RuntimeError('workspace GNOME Shell failed')
            await asyncio.sleep(.2)
        else:
            raise RuntimeError('workspace did not become ready')
        p = await spawn('dbus-update-activation-environment', 'DBUS_SESSION_BUS_ADDRESS',
                        'WAYLAND_DISPLAY', 'DCONF_PROFILE', 'XDG_SESSION_TYPE', 'XDG_CURRENT_DESKTOP')
        await p.wait()
        p = await spawn('gdbus', 'call', '--address', BUS, '--dest', 'local.takeover.Environment',
                        '--object-path', '/local/takeover/Environment',
                        '--method', 'org.freedesktop.Application.Activate', '{}')
        await p.wait()
        # Independent runtime gives the target its own input-method instance.
        if (RUNTIME / 'activation-env.json').exists():
            await spawn('/usr/bin/fcitx5', child_env=input_method_env())
        private_json(RUNTIME / 'workspace.json', {'shell_pid': shell.pid, 'bus': BUS,
                                                 'wayland': 'wayland-takeover', 'monitor': 'Meta-0'})
        print(json.dumps({'event': 'workspace_ready', 'shell_pid': shell.pid}), flush=True)
        notify = os.environ.get('NOTIFY_SOCKET')
        if notify:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.connect('\0' + notify[1:] if notify.startswith('@') else notify)
                s.sendall(b'READY=1\nSTATUS=Shared GNOME workspace ready')
        tasks = [asyncio.create_task(stop.wait()), asyncio.create_task(bus.wait()), asyncio.create_task(shell.wait())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if not stop.is_set():
            raise RuntimeError('persistent workspace process exited unexpectedly')
    finally:
        (RUNTIME / 'workspace.json').unlink(missing_ok=True)
        for p in reversed(children):
            if p.returncode is None:
                p.terminate()
        for p in reversed(children):
            if p.returncode is None:
                try:
                    await asyncio.wait_for(p.wait(), 5)
                except asyncio.TimeoutError:
                    p.kill()


if __name__ == '__main__':
    asyncio.run(main())
