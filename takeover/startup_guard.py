"""Coordinate guarded startup without blocking GNOME Shell's start job.

This module never unlocks a desktop, removes the root guard, or admits a remote
lease. Mutter owns authentication and privacy; the system gateway independently
checks a local authentication before acknowledging local-reclaim.
"""
import errno
import json
import socket
import time


READY_STATES = frozenset(('local', 'ready-guarded'))
STARTUP_STATES = frozenset(('boot-guard', 'startup-locking', 'startup-failed'))
STATUS_KEYS = frozenset(('state', 'screensaver-known', 'screensaver-active',
                        'logind-known', 'logind-locked', 'lock-request-pending',
                        'elapsed-ms', 'reason', 'local-authenticated',
                        'has-owner', 'owner-present', 'remote-inhibited', 'session-path'))


class UnsafePrivacyState(RuntimeError):
    pass


class LocalReclaimRejected(RuntimeError):
    pass


def status_snapshot(read_status):
    if read_status is None:
        return {}
    try:
        status = read_status()
        return {key: value for key, value in status.items() if key in STATUS_KEYS}
    except Exception as error:
        return {'status-error': str(error)}


def wait_startup_ready(read_state, prepare, *, read_status=None, timeout=35,
                       wait_for_recovery=False, heartbeat=lambda: None,
                       log=print, clock=time.monotonic, sleep=time.sleep):
    """Wait only for explicitly identified startup states.

    Active confirmation has a finite budget. The standalone helper returns
    None on expiry/failure. The supervisor instead stays alive in a passive
    recovery mode, observing Mutter's retry or local PAM authentication; it
    never retries PrepareLogin, creates an RDP backend, or weakens privacy in
    that mode. Ordinary locking/remote/blanking are always rejected.
    """
    deadline = clock() + timeout
    prepared = False
    seen_startup = False
    last_state = None
    passive = False
    while True:
        heartbeat()
        state = read_state()
        if state in READY_STATES:
            if seen_startup:
                log(json.dumps({'event': 'native-startup-ready', 'state': state,
                                'status': status_snapshot(read_status)}))
            return state
        if state not in STARTUP_STATES:
            raise UnsafePrivacyState('Refusing to adopt an unrelated native privacy state: ' + str(state))
        seen_startup = True
        if state != last_state:
            log(json.dumps({'event': 'native-startup-state', 'state': state,
                            'status': status_snapshot(read_status)}))
            last_state = state
        if state == 'boot-guard' and not prepared:
            # PrepareLogin is idempotent for the same validated startup session.
            # Another independent helper may have issued it between these calls.
            prepared = True
            try:
                prepare()
            except Exception:
                if read_state() not in (STARTUP_STATES - {'boot-guard'}) | READY_STATES:
                    raise
        if state == 'startup-failed' or clock() >= deadline:
            if not passive:
                log(json.dumps({'event': 'native-startup-wait-expired', 'state': state,
                                'local_recovery_preserved': True,
                                'status': status_snapshot(read_status)}))
                passive = True
            if not wait_for_recovery:
                return None
        sleep(1 if passive else .25)


def request_local_reclaim(session, *, path='/run/ubuntu-native-login/control.sock',
                           timeout=10):
    request = json.dumps({'command': 'local-reclaim', 'role': 'desktop',
                          'session': session}).encode() + b'\n'
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(timeout)
        connection.connect(path)
        connection.settimeout(max(.001, deadline - time.monotonic()))
        connection.sendall(request)
        response = b''
        while b'\n' not in response and len(response) < 4096:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Local reclaim response timed out')
            connection.settimeout(remaining)
            chunk = connection.recv(4096 - len(response))
            if not chunk:
                raise ConnectionResetError('Gateway closed before local reclaim was acknowledged')
            response += chunk
        if response != b'RECLAIMED\n':
            try:
                rejection = json.loads(response)
            except (ValueError, UnicodeDecodeError):
                rejection = None
            # Only an explicit boolean from the gateway marks its independent
            # verification temporarily unavailable. It never grants recovery.
            if isinstance(rejection, dict) and rejection.get('retryable') is True:
                raise BlockingIOError(errno.EAGAIN,
                                      'Gateway local-authentication verification is temporarily unavailable')
            raise LocalReclaimRejected('System gateway refused local authentication recovery; guard retained')


def finish_local_reclaim(session, read_status, guard_present, *,
                          request=request_local_reclaim, heartbeat=lambda: None,
                          log=print, sleep=time.sleep):
    """Ask root to verify local PAM recovery, before exposing a new backend."""
    delay = .5
    waiting = False
    while guard_present():
        heartbeat()
        status = read_status()
        if status.get('state') != 'local' or status.get('local-authenticated') is not True:
            raise LocalReclaimRejected('A remaining startup guard requires confirmed local authentication')
        try:
            request(session)
        except OSError as error:
            if error.errno not in (None, errno.ENOENT, errno.ECONNREFUSED,
                                   errno.ECONNRESET, errno.EPIPE, errno.ETIMEDOUT,
                                   errno.EAGAIN):
                raise
            if not waiting:
                log(json.dumps({'event': 'native-local-reclaim-waiting', 'reason': str(error)}))
                waiting = True
            sleep(delay)
            delay = min(delay * 2, 10)
            continue
        log(json.dumps({'event': 'native-local-reclaim-confirmed'}))
        return


def guard_service_template(prefix):
    """Template for a separate start job, ordered after Shell can finish."""
    return f'''[Unit]
Description=Confirm native startup privacy after GNOME Shell is ready
After=org.gnome.Shell@ubuntu.service graphical-session.target
PartOf=graphical-session.target
ConditionPathExists=/run/ubuntu-native-login/boot-guard
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {prefix}/scripts/finish-login-guard.py
TimeoutStartSec=50
Restart=no
'''


def guard_start_hook():
    # '-' prevents a queueing failure from propagating into Shell's OnFailure.
    return 'ExecStartPost=-/usr/bin/systemctl --user --no-block start ubuntu-native-login-guard.service\n'
