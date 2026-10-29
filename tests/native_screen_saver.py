#!/usr/bin/env python3
"""Fault-injectable lock backend, only on the isolated compositor's test bus."""
import json
import os
from pathlib import Path
import sys
from gi.repository import Gio, GLib

path = Path(sys.argv[1])
active = False
locked_hint = False
lock_calls = 0
events = []


def config():
    return json.loads(path.read_text()) if path.exists() else {}


def record(event):
    events.append(dict(event, time_us=GLib.get_monotonic_time()))
    temporary = path.parent / 'screen-saver-events.tmp'
    temporary.write_text(json.dumps(events))
    temporary.replace(path.parent / 'screen-saver-events.json')


def set_active(value):
    global active
    active = value
    record({'active': value})
    return GLib.SOURCE_REMOVE


def set_hint(value):
    global locked_hint
    locked_hint = value
    record({'locked_hint': value})
    return GLib.SOURCE_REMOVE


def method(conn, sender, obj, interface, method, parameters, invocation):
    global lock_calls
    cfg = config()
    if method == 'GetActive':
        if cfg.get('fail_active_read'):
            invocation.return_dbus_error('org.gnome.Test.Unavailable', 'Injected ScreenSaver unavailability')
            return
        invocation.return_value(GLib.Variant('(b)', (active,)))
        return
    if method == 'Lock':
        lock_calls += 1
        record({'method': 'Lock', 'count': lock_calls})
        if not cfg.get('fail_lock'):
            GLib.timeout_add(cfg.get('lock_delay_ms', 30), set_active, True)
        if not cfg.get('fail_hint') and lock_calls >= cfg.get('hint_after_lock', 1):
            GLib.timeout_add(cfg.get('hint_delay_ms', 30), set_hint, True)
    elif method == 'SetActive':
        value = parameters.unpack()[0]
        record({'method': 'SetActive', 'value': value})
        if not cfg.get('fail_unlock'):
            GLib.timeout_add(cfg.get('unlock_delay_ms', 30), set_active, value)
        if not cfg.get('fail_hint_unlock'):
            GLib.timeout_add(cfg.get('hint_unlock_delay_ms', 30), set_hint, value)
    invocation.return_value(None)


conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
info = Gio.DBusNodeInfo.new_for_xml('''<node><interface name="org.gnome.ScreenSaver">
<method name="Lock"/><method name="SetActive"><arg type="b" direction="in"/></method>
<method name="GetActive"><arg type="b" direction="out"/></method></interface></node>''')
if '--logind-only' not in sys.argv:
    conn.register_object('/org/gnome/ScreenSaver', info.interfaces[0], method, None, None)
    Gio.bus_own_name_on_connection(conn, 'org.gnome.ScreenSaver', Gio.BusNameOwnerFlags.DO_NOT_QUEUE, None, None)
else:
    def follow_real_saver():
        def done(connection, result):
            try:
                value = connection.call_finish(result).unpack()[0]
                if not config().get('fail_hint'):
                    set_hint(value)
            except GLib.Error:
                pass
        conn.call('org.gnome.ScreenSaver', '/org/gnome/ScreenSaver', 'org.gnome.ScreenSaver',
                  'GetActive', None, None, 0, 500, None, done)
        return GLib.SOURCE_CONTINUE
    GLib.timeout_add(250, follow_real_saver)

session = Gio.DBusNodeInfo.new_for_xml('''<node><interface name="org.freedesktop.login1.Session">
<property name="LockedHint" type="b" access="read"/></interface></node>''')
def property_value(*args):
    if config().get('fail_hint_read'):
        return None
    return GLib.Variant('b', locked_hint)
for object_path in ('/test', '/org/freedesktop/login1/session/test'):
    conn.register_object(object_path, session.interfaces[0], None, property_value, None)
Gio.bus_own_name_on_connection(conn, 'org.freedesktop.login1', Gio.BusNameOwnerFlags.DO_NOT_QUEUE, None, None)
GLib.MainLoop().run()
