#!/usr/bin/env python3
"""Legacy shared-workspace helpers retained for reference and regression tests.

The 3391 CLI is disabled in the native 3392 release; its viewer and installation
entrypoints are not distributed. See docs/DEPLOYMENT.md for the native setup.
"""
import argparse
import json
import os
import socket
import subprocess
import time

from takeover.common import BUS, CONFIG, ROOT, RUNTIME, grd_env, private_json, workspace_env


def request(verb):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(15)
        s.connect(str(RUNTIME / 'gate.sock'))
        s.sendall(verb.encode() + b'\n')
        return s.recv(4096).decode().strip()


def register():
    # Desktop-file launchers can inherit the manager session id. Find the live seat.
    sessions = json.loads(subprocess.check_output(['loginctl', 'list-sessions', '--json=short']))
    candidates = []
    for entry in sessions:
        if entry.get('uid') != os.getuid() or entry.get('seat') != 'seat0':
            continue
        sid = str(entry['session'])
        props = dict(line.split('=', 1) for line in subprocess.check_output(
            ['loginctl', 'show-session', sid, '-p', 'State', '-p', 'Type', '-p', 'Active'], text=True).splitlines())
        if props.get('Type') == 'wayland' and props.get('State') != 'closing':
            candidates.append((props.get('Active') == 'yes', sid))
    if not candidates:
        raise RuntimeError('No physical GNOME Wayland login exists')
    sid = sorted(candidates, reverse=True)[0][1]
    # Explicit manager environment also lets setup register safely from SSH.
    display_env = dict(line.split('=', 1) for line in subprocess.check_output(
        ['systemctl', '--user', 'show-environment'], text=True).splitlines() if '=' in line)
    cfg = {'session': sid, 'bus': f'unix:path=/run/user/{os.getuid()}/bus'}
    for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY'):
        cfg[key] = display_env.get(key, '')
    if not cfg['WAYLAND_DISPLAY'] or cfg['WAYLAND_DISPLAY'] == 'wayland-takeover':
        raise RuntimeError('Physical Wayland display missing or points into the workspace')
    private_json(CONFIG / 'local.json', cfg)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['local', 'register', 'status', 'grd', 'rdp', 'broker', 'run'])
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action in ('local', 'register'):
        register()
        if args.action == 'register':
            return
        subprocess.run(['systemctl', '--user', 'start', 'ubuntu-takeover-broker.service',
                        'ubuntu-takeover-rdp.service'], check=True)
        for _ in range(50):
            if (RUNTIME / 'gate.sock').exists():
                break
            time.sleep(.1)
        reply = request('LOCAL')
        if reply != 'OK':
            raise RuntimeError('请先用 Ubuntu 密码解锁本地桌面，再打开共享工作桌面。')
    elif args.action == 'status':
        print(request('STATUS'))
    elif args.action in ('grd', 'rdp'):
        os.execve(ROOT / 'build/grd-prefix/libexec/gnome-remote-desktop-daemon',
                  ['gnome-remote-desktop-daemon', '--headless', '--rdp-port=3391'], grd_env())
    elif args.action == 'broker':
        os.execv('/usr/bin/python3', ['python3', '-m', 'takeover.broker', '--socket', str(RUNTIME / 'gate.sock'),
                 '--local-registry', str(CONFIG / 'local.json'), '--status', str(RUNTIME / 'status.json'), '--',
                 str(ROOT / 'scripts/session-viewer'), '--attach-monitor', 'Meta-0', '--target-bus', BUS, '--fullscreen'])
    elif args.action == 'run':
        if not args.command:
            parser.error('run requires a command')
        os.execvpe(args.command[0], args.command, workspace_env())


if __name__ == '__main__':
    raise SystemExit('The legacy 3391 CLI is not supported by this release. See docs/DEPLOYMENT.md.')
