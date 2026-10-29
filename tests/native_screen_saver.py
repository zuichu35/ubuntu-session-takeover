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
<method name="SetLockedHint"><arg type="b" direction="in"/></method>
<method name="Activate"/>
<property name="LockedHint" type="b" access="read"/>
<property name="Id" type="s" access="read"/>
<property name="Active" type="b" access="read"/>
<property name="Class" type="s" access="read"/>
<property name="Type" type="s" access="read"/>
<property name="State" type="s" access="read"/>
<property name="Remote" type="b" access="read"/>
<property name="User" type="(uo)" access="read"/>
<property name="Seat" type="(so)" access="read"/>
<signal name="Lock"/><signal name="Unlock"/>
</interface></node>''')
def property_value(*args):
    name = args[-1]
    if name == 'LockedHint' and config().get('fail_hint_read'):
        return None
    return {'LockedHint': GLib.Variant('b', locked_hint), 'Id': GLib.Variant('s', 'test'),
            'Active': GLib.Variant('b', True), 'Class': GLib.Variant('s', 'user'),
            'Type': GLib.Variant('s', 'wayland'), 'State': GLib.Variant('s', 'active'),
            'Remote': GLib.Variant('b', False),
            'User': GLib.Variant('(uo)', (os.getuid(), '/org/freedesktop/login1/user/test')),
            'Seat': GLib.Variant('(so)', ('seat0', '/org/freedesktop/login1/seat/seat0'))}[name]


def session_method(conn, sender, obj, interface, method, params, invocation):
    if method == 'SetLockedHint' and not config().get('fail_hint'):
        set_hint(params.unpack()[0])
    invocation.return_value(None)


for object_path in ('/test', '/org/freedesktop/login1/session/test'):
    conn.register_object(object_path, session.interfaces[0], session_method, property_value, None)
Gio.bus_own_name_on_connection(conn, 'org.freedesktop.login1', Gio.BusNameOwnerFlags.DO_NOT_QUEUE, None, None)

# Minimal system services for a real GNOME Shell in a container. Every call
# stays on the private test bus; no real login session is created or changed.
manager = Gio.DBusNodeInfo.new_for_xml('''<node><interface name="org.freedesktop.login1.Manager">
<method name="GetSession"><arg type="s" direction="in"/><arg type="o" direction="out"/></method>
<method name="GetUser"><arg type="u" direction="in"/><arg type="o" direction="out"/></method>
<method name="ListSessions"><arg type="a(susso)" direction="out"/></method>
<method name="Inhibit"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="h" direction="out"/></method>
<property name="PreparingForSleep" type="b" access="read"/>
</interface></node>''')


def manager_method(conn, sender, obj, interface, method, params, invocation):
    if method == 'GetSession':
        invocation.return_value(GLib.Variant('(o)', ('/org/freedesktop/login1/session/test',)))
    elif method == 'GetUser':
        invocation.return_value(GLib.Variant('(o)', ('/org/freedesktop/login1/user/test',)))
    elif method == 'ListSessions':
        invocation.return_value(GLib.Variant('(a(susso))', ([('test', os.getuid(), 'fixture', 'seat0', '/org/freedesktop/login1/session/test')],)))
    elif method == 'Inhibit':
        fd = os.open('/dev/null', os.O_RDONLY)
        try:
            descriptors = Gio.UnixFDList.new()
            index = descriptors.append(fd)
            invocation.return_value_with_unix_fd_list(GLib.Variant('(h)', (index,)), descriptors)
        finally:
            os.close(fd)


conn.register_object('/org/freedesktop/login1', manager.interfaces[0], manager_method,
                     lambda *args: GLib.Variant('b', False), None)
gdm = Gio.DBusNodeInfo.new_for_xml('''<node><interface name="org.gnome.DisplayManager.Manager">
<method name="RegisterSession"/><method name="RegisterDisplay"/>
<property name="Version" type="s" access="read"/></interface></node>''')
conn.register_object('/org/gnome/DisplayManager/Manager', gdm.interfaces[0],
                     lambda c, s, o, i, m, p, call: call.return_value(None),
                     lambda *args: GLib.Variant('s', '50.1'), None)
Gio.bus_own_name_on_connection(conn, 'org.gnome.DisplayManager', Gio.BusNameOwnerFlags.DO_NOT_QUEUE, None, None)
GLib.MainLoop().run()
