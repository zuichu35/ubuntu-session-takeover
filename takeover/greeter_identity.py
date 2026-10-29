"""Identify GDM's current native greeter without caching its dynamic UID.

GDM 49+ allocates transient accounts with the gdm primary group. Group
membership alone is not authorization: logind must also identify the same UID
as the physical Wayland greeter on seat0. Account names are not trusted.
"""
import grp
import os
import pwd


def is_gdm_group_member(uid):
    """Resolve both records each time; a deleted/recycled UID must not persist."""
    try:
        return pwd.getpwuid(uid).pw_gid == grp.getgrnam('gdm').gr_gid
    except (KeyError, OSError):
        return False


def is_native_greeter_session(uid, properties, group_check=None):
    group_check = group_check or is_gdm_group_member
    return bool(
        group_check(uid) and
        properties.get('User', [-1])[0] == uid and
        properties.get('Seat', [''])[0] == 'seat0' and
        properties.get('Type') == 'wayland' and
        properties.get('Class') == 'greeter' and
        properties.get('Remote') is False and
        properties.get('State') != 'closing'
    )


def session_properties(path):
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    return bus.call_sync('org.freedesktop.login1', path,
        'org.freedesktop.DBus.Properties', 'GetAll',
        GLib.Variant('(s)', ('org.freedesktop.login1.Session',)),
        None, 0, 3000, None).unpack()[0]


def logind_sessions():
    from gi.repository import Gio
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    return bus.call_sync('org.freedesktop.login1', '/org/freedesktop/login1',
        'org.freedesktop.login1.Manager', 'ListSessions',
        None, None, 0, 3000, None).unpack()[0]


def find_native_greeter_session(uid=None, list_sessions=None, session_info=None,
                                group_check=None):
    """Return the unique confirmed greeter path, or None; errors fail closed."""
    uid = os.getuid() if uid is None else uid
    group_check = group_check or is_gdm_group_member
    if not group_check(uid):
        return None
    list_sessions = list_sessions or logind_sessions
    session_info = session_info or session_properties
    candidates = []
    try:
        for _, owner, _, seat, path in list_sessions():
            if owner == uid and seat == 'seat0':
                if is_native_greeter_session(uid, session_info(path), group_check):
                    candidates.append(path)
    except Exception:
        # Never infer a privileged greeter role from a missing logind response.
        return None
    return candidates[0] if len(candidates) == 1 else None
