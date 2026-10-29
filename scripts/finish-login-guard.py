#!/usr/bin/python3
"""Bounded lock confirmation in a separate unit after Shell's start job.

Never use this as a blocking Shell ExecStartPost. Queue the independent guard
unit with systemctl --no-block; Mutter already protects the first frame.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from takeover.login_gateway import boot_guard_present
from takeover.greeter_identity import find_native_greeter_session
from takeover.startup_guard import STARTUP_STATES, READY_STATES, wait_startup_ready
from gi.repository import Gio, GLib


def main():
    # A real greeter has no user desktop to lock. A group match, username or
    # inherited environment flag alone must never bypass the startup guard.
    if find_native_greeter_session() is not None:
        return
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    name = 'org.gnome.Mutter.NativeTakeover'

    def call(method, parameters=None):
        return bus.call_sync(name, '/org/gnome/Mutter/NativeTakeover', name, method,
                             parameters, None, 0, 3000, None).unpack()

    try:
        state = call('GetState')[0]
    except GLib.Error:
        if boot_guard_present():
            raise SystemExit('Missing startup privacy interface during remote login')
        return
    if state in STARTUP_STATES:
        sessions = json.loads(subprocess.check_output(['loginctl', 'list-sessions', '--json=short']))
        candidates = [s for s in sessions if s['uid'] == os.getuid() and
                      s.get('seat') == 'seat0' and s.get('class') == 'user']
        if len(candidates) != 1:
            raise SystemExit('Cannot identify the physical session; startup privacy remains active')
        system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        path = system.call_sync('org.freedesktop.login1', '/org/freedesktop/login1',
            'org.freedesktop.login1.Manager', 'GetSession',
            GLib.Variant('(s)', (str(candidates[0]['session']),)), None, 0, 3000, None).unpack()[0]
        result = wait_startup_ready(lambda: call('GetState')[0],
            lambda: call('PrepareLogin', GLib.Variant('(o)', (path,))),
            read_status=lambda: call('GetStatus')[0],
            log=lambda message: print(message, flush=True))
        if result is None:
            # This unit has no OnFailure dependency on Shell. Timeout leaves
            # Mutter's privacy/PAM recovery running and never logs out the user.
            print(json.dumps({'event': 'native-startup-helper-finished',
                              'ready': False, 'privacy_preserved': True}), flush=True)
    elif state not in READY_STATES:
        print(json.dumps({'event': 'native-startup-helper-skipped', 'state': state,
                          'reason': 'another privacy transition owns the desktop'}), flush=True)


if __name__ == '__main__':
    main()
