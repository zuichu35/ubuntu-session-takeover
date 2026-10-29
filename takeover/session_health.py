"""Non-invasive readiness and recovery helpers for the native supervisor."""
from collections import OrderedDict
import errno
import json
import os
from pathlib import Path
import select
import socket
import time


_readiness_fallback_logged = OrderedDict()


def _live_same_uid_process(pid, proc):
    """Return a live process identity without requiring ptrace/dumpable access."""
    try:
        status = dict(line.split(':', 1) for line in
                      (proc / str(pid) / 'status').read_text().splitlines() if ':' in line)
        fields = (proc / str(pid) / 'stat').read_text().rpartition(')')[2].split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    try:
        uids = [int(value) for value in status['Uid'].split()]
        status_state = status['State'].split()[0]
        core_dumping = int(status.get('CoreDumping', '0').strip())
        process_state, start_time = fields[0], int(fields[19])
    except (KeyError, ValueError, IndexError) as error:
        raise RuntimeError(f'Cannot verify RDP backend process identity for PID {pid}') from error
    if core_dumping or status_state in ('Z', 'X', 'x') or process_state in ('Z', 'X', 'x'):
        return None
    # The supervisor launches GRD without changing UID. A privileged or foreign
    # account is not an acceptable substitute for its unreadable fd table.
    if len(uids) != 4 or any(uid != os.geteuid() for uid in uids):
        return None
    return uids[1], start_time


def _log_readiness_fallback(pid, port, identity, proc, log):
    key = (str(proc), pid, identity)
    if key in _readiness_fallback_logged:
        return
    _readiness_fallback_logged[key] = None
    # A long-lived supervisor must not retain an unbounded history of children.
    if len(_readiness_fallback_logged) > 64:
        _readiness_fallback_logged.popitem(last=False)
    message = json.dumps({'event': 'native-rdp-readiness-uid-fallback',
                          'reason': 'proc-fd-permission-denied', 'pid': pid,
                          'port': port, 'uid': identity[0],
                          'ownership': 'same-uid-loopback-listener',
                          'child_state': 'alive-not-coredumping'})
    if log is None:
        print(message, flush=True)
    else:
        log(message)


def backend_listening(port, pid, proc=Path('/proc'), *, log=None):
    """Check readiness without opening an RDP connection or requiring ptrace.

    Normally the listening socket inode must appear in this child's fd table.
    Non-dumpable GRD processes can deny fd inspection even to their parent. Only
    on that PermissionError, accept an exact loopback listener owned by the same
    UID, while confirming that the original same-UID child is alive before and
    after the lookup. This fallback establishes UID ownership, not exact PID
    ownership; it uses the existing same-account trust boundary and is logged.
    Other procfs errors are not silently converted into readiness.
    """
    inodes = set()
    identity = None
    try:
        for fd in (proc / str(pid) / 'fd').iterdir():
            try:
                target = os.readlink(fd)
            except FileNotFoundError:  # A socket may close while enumerating it.
                continue
            if target.startswith('socket:[') and target.endswith(']'):
                inodes.add(target[8:-1])
    except FileNotFoundError:
        return False
    except PermissionError:
        inodes.clear()
        identity = _live_same_uid_process(pid, proc)
        if identity is None:
            return False
        _log_readiness_fallback(pid, port, identity, proc, log)
    for name, loopback in (('tcp', '0100007F'),
                           ('tcp6', '00000000000000000000000001000000')):
        path = proc / 'net' / name
        try:
            rows = path.read_text().splitlines()[1:]
        except FileNotFoundError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) < 10 or fields[3] != '0A':
                continue
            address, local_port = fields[1].rsplit(':', 1)
            if address != loopback or int(local_port, 16) != port:
                continue
            if identity is None:
                if fields[9] in inodes:
                    return True
            elif int(fields[7]) == identity[0]:
                return _live_same_uid_process(pid, proc) == identity
    return False


class GatewayRejected(RuntimeError):
    """Explicit rejection is fatal; transport outages are retried separately."""


class GatewayRegistration:
    def __init__(self, path, role, session, *, clock=time.monotonic, log=print,
                 timeout=5, maximum_delay=10):
        self.path = str(path)
        self.request = (json.dumps({'command': 'register', 'role': role,
                                   'session': session}) + '\n').encode()
        self.clock, self.log = clock, log
        self.timeout, self.maximum_delay = timeout, maximum_delay
        self.connection = None
        self.next_attempt = 0
        self.delay = .5
        self.waiting = False

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _retry(self, error):
        self.close()
        if not self.waiting:
            self.log(json.dumps({'event': 'native-gateway-waiting', 'reason': str(error)}))
        self.waiting = True
        self.next_attempt = self.clock() + self.delay
        self.delay = min(self.delay * 2, self.maximum_delay)

    def step(self):
        """Maintain registration; each attempt blocks for at most timeout seconds.

        The caller keeps supervising children and processing signals between
        attempts. No new session or greeter fallback is selected during retry.
        """
        if self.connection is not None:
            try:
                if not select.select([self.connection], [], [], 0)[0]:
                    return True
                data = self.connection.recv(4096)
            except (ConnectionError, OSError) as error:
                self._retry(error)
            else:
                if data:
                    self.close()
                    raise GatewayRejected('System gateway sent unexpected registration data')
                self._retry('System login gateway disconnected')
        if self.clock() < self.next_attempt:
            return False
        connection = socket.socket(socket.AF_UNIX)
        try:
            deadline = self.clock() + self.timeout
            connection.settimeout(self.timeout)
            connection.connect(self.path)
            connection.settimeout(max(.001, deadline - self.clock()))
            connection.sendall(self.request)
            response = b''
            while b'\n' not in response and len(response) < 4096:
                if self.clock() >= deadline:
                    raise TimeoutError('System gateway registration timed out')
                connection.settimeout(max(.001, deadline - self.clock()))
                data = connection.recv(4096 - len(response))
                if not data:
                    raise ConnectionResetError('System gateway closed before registration completed')
                response += data
            if response != b'READY\n':
                raise GatewayRejected('System gateway rejected native endpoint')
            connection.setblocking(False)
            self.connection = connection
            connection = None
            self.delay = .5
            self.next_attempt = 0
            self.log(json.dumps({'event': 'native-gateway-registered', 'recovered': self.waiting}))
            self.waiting = False
            return True
        except OSError as error:
            if error.errno not in (None, errno.ENOENT, errno.ECONNREFUSED,
                                   errno.ECONNRESET, errno.EPIPE, errno.ETIMEDOUT,
                                   errno.EAGAIN):
                raise
            self._retry(error)
            return False
        finally:
            if connection is not None:
                connection.close()


class Watchdog:
    def __init__(self, environment=None, clock=time.monotonic):
        environment = os.environ if environment is None else environment
        self.address = environment.get('NOTIFY_SOCKET')
        usec = int(environment.get('WATCHDOG_USEC', '0'))
        owner = int(environment.get('WATCHDOG_PID', str(os.getpid())))
        # Five-second registration waits can fall between heartbeat polls.
        self.interval = min(usec / 2000000, 5) if owner == os.getpid() else 0
        self.clock, self.next_ping = clock, 0

    def ping(self):
        if not self.address or not self.interval or self.clock() < self.next_ping:
            return
        address = '\0' + self.address[1:] if self.address.startswith('@') else self.address
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
            notifier.settimeout(.2)
            notifier.connect(address)
            notifier.sendall(b'WATCHDOG=1')
        self.next_ping = self.clock() + self.interval
