"""Exercise the local authenticated-daemon gate on an already locked/off seat.

Does not test Windows authentication or capture physical pixels. Refuses to run
on an unlocked desktop or while a remote lease is active. This changes the live
desktop's privacy/display state and requires the explicit --confirm-live flag.
"""
import argparse
import json
import os
import socket
import time
from pathlib import Path
from gi.repository import Gio, GLib

bus = None
system = None
gate = f'/run/user/{os.getuid()}/ubuntu-native-takeover/gate.sock'
display = 'org.gnome.Mutter.DisplayConfig'
properties = 'org.freedesktop.DBus.Properties'


def call(name, path, interface, method, args=None, connection=None):
    connection = bus if connection is None else connection
    return connection.call_sync(name, path, interface, method, args, None, 0, 3000, None).unpack()


def power():
    return call(display, '/org/gnome/Mutter/DisplayConfig', properties, 'Get',
                GLib.Variant('(ss)', (display, 'PowerSaveMode')))[0]


def connect():
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(12)
    sock.connect(gate)
    return sock


def status():
    with connect() as sock:
        sock.sendall(b'STATUS\n')
        return json.loads(sock.recv(1024))


def locked(session):
    return (call('org.gnome.ScreenSaver', '/org/gnome/ScreenSaver',
                 'org.gnome.ScreenSaver', 'GetActive')[0] and
            call('org.freedesktop.login1', session, properties, 'Get',
                 GLib.Variant('(ss)', ('org.freedesktop.login1.Session', 'LockedHint')),
                 connection=system)[0])


def monitors():
    return len(call(display, '/org/gnome/Mutter/DisplayConfig', display, 'GetCurrentState')[2])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm-live', action='store_true',
                        help='Allow this test to control the current desktop and display power')
    if not parser.parse_args(argv).confirm_live:
        parser.error('--confirm-live is required; no desktop connection was made')
    global bus, system
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    initial = status()
    assert initial['native_state'] == 'local' and not initial['remote']
    assert power() == 3 and locked(initial['session']), 'Requires already locked, powered-off displays'
    original_count = monitors()
    started = time.monotonic()
    sock = connect()
    report = {}
    try:
        sock.sendall(b'REMOTE\n')
        reply = sock.recv(64)
        assert reply == b'GRANTED\n', repr(reply)
        report = {'off_to_remote_seconds': round(time.monotonic() - started, 3),
                  'remote_state': status()['native_state'], 'power_during_remote': power(),
                  'remote_monitor_count': monitors()}
        assert report['remote_state'] == 'remote' and report['power_during_remote'] == 0
        assert report['remote_monitor_count'] == 1
    finally:
        sock.close()
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            state = status()
            if state['native_state'] == 'local' and not state['remote'] and locked(initial['session']):
                break
            time.sleep(.1)
        else:
            raise RuntimeError('Local lock recovery did not finish')
        assert monitors() == original_count, 'Monitor restoration failed'
        call(display, '/org/gnome/Mutter/DisplayConfig', properties, 'Set',
             GLib.Variant('(ssv)', (display, 'PowerSaveMode', GLib.Variant('i', 3))))
    report.update(local_lock_confirmed=True, original_monitor_count_restored=original_count,
                  power_restored=power(), windows_rdp_client_tested=False,
                  physical_black_pixels_measured=False)
    output = Path('artifacts/power-save-live-result.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
