"""Read-only startup evidence and a process-bound local fallback receipt.

The receipt only disables the native backend for the current system Shell. It
never grants remote access, clears a privacy guard or authenticates a user.
The current UID is the existing trust boundary; stale PIDs are not evidence.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading

from .greeter_identity import is_gdm_group_member


GUARD = Path('/run/ubuntu-native-login/boot-guard')
RUNTIME = Path('/run/user') / str(os.getuid()) / 'ubuntu-native-takeover'
PROC = Path('/proc')
RECEIPT = 'shell-launch.json'
COMPOSITOR_NAMES = {'org.gnome.Shell', 'org.gnome.Mutter.NativeTakeover',
                    'org.gnome.Mutter.DisplayConfig'}


class StartupRefused(RuntimeError):
    pass


def guard_state(path=None):
    """Absence is established only by ENOENT, never by an invalid guard."""
    target = Path(GUARD if path is None else path)
    for parent in target.parents:
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return 'unknown'
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            return 'unknown'
    try:
        info = target.lstat()
    except FileNotFoundError:
        return 'absent'
    except OSError:
        return 'unknown'
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        return 'unknown'
    return 'present'


def _connect(address):
    from gi.repository import Gio
    cancellable = Gio.Cancellable()
    timer = threading.Timer(2, cancellable.cancel)
    timer.daemon = True
    timer.start()
    try:
        return Gio.DBusConnection.new_for_address_sync(address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
            Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, cancellable)
    finally:
        timer.cancel()


def _call(bus, name, path, interface, method, parameters=None):
    from gi.repository import Gio
    return bus.call_sync(name, path, interface, method, parameters, None,
                         Gio.DBusCallFlags.NO_AUTO_START, 1500, None).unpack()[0]


def _names(bus):
    result = _call(bus, 'org.freedesktop.DBus', '/org/freedesktop/DBus',
                   'org.freedesktop.DBus', 'ListNames')
    if not isinstance(result, (list, tuple)) or any(not isinstance(v, str) for v in result):
        raise StartupRefused('Invalid session-bus owner list')
    return set(result)


def current_session(uid=None):
    """Require one real, local Wayland session; do not infer from environment."""
    from gi.repository import GLib
    uid = os.getuid() if uid is None else uid
    system = _connect('unix:path=/run/dbus/system_bus_socket')
    sessions = _call(system, 'org.freedesktop.login1', '/org/freedesktop/login1',
                     'org.freedesktop.login1.Manager', 'ListSessions')
    physical = [entry for entry in sessions if entry[1] == uid and entry[3] == 'seat0']
    if len(physical) > 4:
        raise StartupRefused('Ambiguous physical session list')
    matches = []
    gdm = is_gdm_group_member(uid)
    for _, _, _, _, path in physical:
        props = _call(system, 'org.freedesktop.login1', path, 'org.freedesktop.DBus.Properties',
                      'GetAll', GLib.Variant('(s)', ('org.freedesktop.login1.Session',)))
        role = 'greeter' if gdm and props.get('Class') == 'greeter' else 'desktop'
        allowed_class = props.get('Class') == 'greeter' if role == 'greeter' else props.get('Class') in ('user', 'user-early')
        if (allowed_class and props.get('User', [-1])[0] == uid and
                props.get('Seat', [''])[0] == 'seat0' and props.get('Type') == 'wayland' and
                props.get('Remote') is False and props.get('State') != 'closing'):
            matches.append({'session': path, 'role': role})
    if len(matches) != 1:
        raise StartupRefused('Exactly one physical Wayland session must be confirmed by logind')
    return matches[0]


def inspect_startup():
    report = {'known': False, 'guard': guard_state(), 'owners': [],
              'session': None, 'role': None, 'reasons': []}
    try:
        session = current_session()
        bus = _connect(f'unix:path=/run/user/{os.getuid()}/bus')
        report.update(session, owners=sorted(_names(bus) & COMPOSITOR_NAMES))
        report['known'] = report['guard'] in ('absent', 'present')
        if not report['known']:
            report['reasons'].append('Startup privacy guard cannot be verified')
    except Exception as error:
        report['reasons'].append('Read-only startup evidence unavailable: ' + type(error).__name__)
    return report


def choose_mode(candidate, context):
    if (context.get('known') is not True or context.get('guard') not in ('absent', 'present') or
            not context.get('session') or context.get('role') not in ('desktop', 'greeter')):
        raise StartupRefused('Cannot prove the current session and privacy state are safe')
    if context.get('owners') != []:
        raise StartupRefused('An existing compositor or remote-control owner prevents a second Shell')
    if candidate.get('status') == 'verified':
        return 'private'
    if context['guard'] != 'absent':
        raise StartupRefused('Candidate is not verified while startup privacy is required')
    return 'system-fallback'


def _private_path(value, root, report):
    if not value:
        return False
    path = Path(value)
    prefixes = [Path(root)]
    for key in ('runtime_root', 'native_prefix', 'grd_prefix', 'library_dir',
                'mutter_library_dir', 'typelib_dir'):
        entry = report.get('paths', {}).get(key)
        if isinstance(entry, str) and Path(entry).is_absolute():
            prefixes.append(Path(entry))
    for normalized in (Path(os.path.abspath(path)), path.resolve()):
        if any(normalized == base or base in normalized.parents for base in prefixes):
            return True
        # Remove earlier installed versions and all private Mutter ABI dirs.
        if any(part.startswith('ubuntu-native-takeover') or part in ('native-prefix', 'grd-prefix')
               for part in normalized.parts):
            return True
    return False


def clean_environment(environment, root, report):
    """Preserve unrelated wrapper paths; remove this project's loader inputs."""
    result = {k: v for k, v in environment.items()
              if not k.startswith(('MUTTER_NATIVE_', 'GRD_NATIVE_', 'UBUNTU_NATIVE_SESSION_'))}
    for key in ('LD_LIBRARY_PATH', 'GI_TYPELIB_PATH', 'LD_PRELOAD', 'LD_AUDIT'):
        if key not in result:
            continue
        parts = re.split(r'[:\s]+', result[key]) if key in ('LD_PRELOAD', 'LD_AUDIT') else result[key].split(':')
        kept = [p for p in parts if p and not _private_path(p, root, report)]
        if kept:
            result[key] = ':'.join(kept)
        else:
            result.pop(key, None)
    return result


def _safe_directory(path, uid):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
        raise StartupRefused('Unsafe per-user launch receipt directory')


def launch_directory():
    _safe_directory(RUNTIME.parent, os.getuid())
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    _safe_directory(RUNTIME, os.getuid())
    return RUNTIME


def acquire_launch_lock():
    directory = launch_directory()
    fd = os.open(directory / 'shell-launch.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise StartupRefused('Unsafe per-user Shell launch lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.set_inheritable(fd, True)  # Hold across exec until this Shell exits.
        return fd
    except Exception:
        os.close(fd)
        raise


def _process_identity(pid, proc=None):
    proc = PROC if proc is None else Path(proc)
    directory = proc / str(pid)
    info = directory.stat()
    fields = (directory / 'stat').read_text().rpartition(')')[2].split()
    if len(fields) <= 19 or fields[0] in ('Z', 'X', 'x'):
        raise StartupRefused('Shell launch process is no longer live')
    return {'uid': info.st_uid, 'pid': pid, 'start_time': int(fields[19])}


def _boot_id():
    return (PROC / 'sys/kernel/random/boot_id').read_text().strip()


def write_launch_receipt(mode, context):
    record = {'version': 1, 'mode': mode, 'session': context['session'], 'role': context['role'],
              'boot_id': _boot_id(), **_process_identity(os.getpid())}
    directory = launch_directory()
    fd, temporary = tempfile.mkstemp(prefix='.shell-launch-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(record, stream)
            stream.write('\n')
        os.replace(temporary, directory / RECEIPT)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return record


def confirmed_system_fallback(bus=None):
    """Only an exact current Shell receipt suppresses a missing native backend."""
    try:
        if guard_state() != 'absent':
            return False
        _safe_directory(RUNTIME.parent, os.getuid())
        _safe_directory(RUNTIME, os.getuid())
        directory = RUNTIME
        fd = os.open(directory / RECEIPT, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_mode & 0o077 or info.st_size > 8192):
                return False
            record = json.load(stream)
        if (record.get('version') != 1 or record.get('mode') != 'system-fallback' or
                record.get('boot_id') != _boot_id() or record.get('uid') != os.getuid() or
                type(record.get('pid')) is not int or record['pid'] <= 0):
            return False
        identity = _process_identity(record['pid'])
        if any(record.get(key) != value for key, value in identity.items()):
            return False
        session = current_session()
        if any(record.get(key) != value for key, value in session.items()):
            return False
        bus = bus or _connect(f'unix:path=/run/user/{os.getuid()}/bus')
        names = _names(bus)
        if 'org.gnome.Mutter.NativeTakeover' in names or 'org.gnome.Shell' not in names:
            return False
        from gi.repository import GLib
        parameters = GLib.Variant('(s)', ('org.gnome.Shell',))
        def owner(method):
            return _call(bus, 'org.freedesktop.DBus', '/org/freedesktop/DBus',
                         'org.freedesktop.DBus', method, parameters)
        if owner('GetConnectionUnixUser') != os.getuid() or owner('GetConnectionUnixProcessID') != record['pid']:
            return False
        # Recheck against logout, PID replacement, and a newly armed guard.
        return (_process_identity(record['pid']) == identity and guard_state() == 'absent' and
                owner('GetConnectionUnixProcessID') == record['pid'] and
                'org.gnome.Mutter.NativeTakeover' not in _names(bus))
    except Exception:
        return False
