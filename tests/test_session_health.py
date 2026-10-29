import errno
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from takeover.session_health import backend_listening, GatewayRegistration, GatewayRejected, Watchdog


class LocalGateway:
    def __init__(self, path, reply=b'READY\n', reply_delay=0):
        self.path, self.reply, self.reply_delay = path, reply, reply_delay
        self.requests, self.peers = [], []
        self.stopping = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(path))
        self.listener.listen()
        self.listener.settimeout(.05)
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def run(self):
        while not self.stopping.is_set():
            try:
                peer, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            self.peers.append(peer)
            peer.settimeout(.5)
            try:
                request = b''
                while b'\n' not in request:
                    data = peer.recv(4096)
                    if not data:
                        break
                    request += data
                self.requests.append(json.loads(request))
                self.stopping.wait(self.reply_delay)
                # Deliberately fragment READY to exercise stream framing.
                peer.sendall(self.reply[:2])
                peer.sendall(self.reply[2:])
            except (OSError, ValueError):
                pass

    def close(self):
        self.stopping.set()
        self.listener.close()
        self.worker.join(1)
        for peer in self.peers:
            peer.close()
        self.path.unlink(missing_ok=True)


class SessionHealthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.clock = Mock(return_value=100.)
        self.registration = GatewayRegistration(self.root / 'gateway.sock', 'desktop',
            '/org/freedesktop/login1/session/_343', clock=self.clock, log=Mock())
        self.addCleanup(self.registration.close)

    def gateway(self, reply=b'READY\n', reply_delay=0):
        gateway = LocalGateway(self.root / 'gateway.sock', reply, reply_delay)
        self.addCleanup(gateway.close)
        return gateway

    def test_gateway_outage_recovers_with_the_same_verified_session(self):
        first = self.gateway()
        self.assertTrue(self.registration.step())
        self.assertEqual(first.requests[0]['session'], '/org/freedesktop/login1/session/_343')
        first.close()
        self.assertFalse(self.registration.step())
        self.assertIsNone(self.registration.connection)
        self.clock.return_value = 100.5
        self.assertFalse(self.registration.step())
        second = self.gateway()
        self.clock.return_value = 101.5
        self.assertTrue(self.registration.step())
        self.assertEqual(second.requests, first.requests)
        self.assertFalse(self.registration.waiting)

    def test_startup_without_gateway_uses_capped_backoff(self):
        for expected_delay in (.5, 1, 2, 4, 8, 10, 10):
            now = self.clock.return_value
            self.assertFalse(self.registration.step())
            self.assertEqual(self.registration.next_attempt - now, expected_delay)
            self.assertFalse(self.registration.step())
            self.clock.return_value = self.registration.next_attempt
        self.assertEqual(self.registration.log.call_count, 1)

    def test_gateway_rejection_is_fatal_and_does_not_change_role(self):
        gateway = self.gateway(b'{"error":"wrong peer UID"}\n')
        with self.assertRaises(GatewayRejected):
            self.registration.step()
        self.assertIsNone(self.registration.connection)
        self.assertFalse(self.registration.waiting)
        self.assertEqual(gateway.requests[0]['role'], 'desktop')

    def test_permission_failure_is_not_treated_as_transport_outage(self):
        connection = Mock()
        connection.connect.side_effect = PermissionError(errno.EACCES, 'Permission denied')
        with patch('takeover.session_health.socket.socket', return_value=connection):
            with self.assertRaises(PermissionError):
                self.registration.step()
        connection.close.assert_called_once()

    def test_unresponsive_gateway_has_bounded_attempt_and_can_recover(self):
        gateway = self.gateway(reply_delay=.1)
        self.registration.timeout = .02
        started = time.monotonic()
        self.assertFalse(self.registration.step())
        self.assertLess(time.monotonic() - started, .3)
        self.assertIsNone(self.registration.connection)
        gateway.close()
        self.gateway()
        self.clock.return_value = self.registration.next_attempt
        self.assertTrue(self.registration.step())

    def test_signal_interrupt_closes_inflight_connection_and_propagates(self):
        connection = Mock()
        connection.recv.side_effect = KeyboardInterrupt
        with patch('takeover.session_health.socket.socket', return_value=connection):
            with self.assertRaises(KeyboardInterrupt):
                self.registration.step()
        connection.close.assert_called_once()
        self.assertIsNone(self.registration.connection)

    def test_listener_readiness_never_connects_to_rdp(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
            self.assertFalse(backend_listening(port, os.getpid()))
            listener.listen()
            self.assertTrue(backend_listening(port, os.getpid()))
            listener.settimeout(.05)
            with self.assertRaises(TimeoutError):
                listener.accept()

    def test_listener_must_belong_to_child_and_be_loopback(self):
        (self.root / '123/fd').mkdir(parents=True)
        (self.root / '123/fd/5').symlink_to('socket:[456]')
        (self.root / 'net').mkdir()
        tcp = self.root / 'net/tcp'
        prefix = 'header\n'
        def row(address='0100007F', state='0A', inode='456'):
            return prefix + f'0: {address}:0D41 00000000:0000 {state} 0 0 0 0 0 {inode}\n'
        for address, state, inode in [('00000000', '0A', '456'),
                                      ('0100007F', '01', '456'),
                                      ('0100007F', '0A', '999')]:
            tcp.write_text(row(address, state, inode))
            self.assertFalse(backend_listening(3393, 123, self.root))
        tcp.write_text(row())
        self.assertTrue(backend_listening(3393, 123, self.root))
        tcp.unlink()
        (self.root / 'net/tcp6').write_text(row('00000000000000000000000001000000'))
        self.assertTrue(backend_listening(3393, 123, self.root))

    def proc_fixture(self, *, uid=None, state='S', start_time=900, core_dumping=0):
        uid = os.geteuid() if uid is None else uid
        (self.root / '123/fd').mkdir(parents=True, exist_ok=True)
        (self.root / 'net').mkdir(exist_ok=True)
        (self.root / '123/status').write_text(f'Name:\tgrd\nState:\t{state}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nCoreDumping:\t{core_dumping}\n')
        (self.root / '123/stat').write_text('123 (grd worker) ' +
            ' '.join([state] + ['0'] * 18 + [str(start_time)]) + '\n')

    def denied_fd_inspection(self):
        original = Path.iterdir
        def iterdir(path):
            if path == self.root / '123/fd':
                raise PermissionError(errno.EACCES, 'non-dumpable child')
            return original(path)
        return patch.object(Path, 'iterdir', iterdir)

    def proc_listener(self, address='0100007F', *, uid=None, state='0A', name='tcp'):
        uid = os.geteuid() if uid is None else uid
        (self.root / 'net' / name).write_text('header\n' +
            f'0: {address}:0D42 00000000:0000 {state} 0 0 0 {uid} 0 456\n')

    def test_fd_permission_fallback_rejects_foreign_uid_wildcard_and_nonlisteners(self):
        self.proc_fixture()
        cases = [('00000000', os.geteuid(), '0A'),
                 ('0100007F', os.geteuid() + 1, '0A'),
                 ('0100007F', os.geteuid(), '01')]
        with self.denied_fd_inspection():
            for address, uid, state in cases:
                self.proc_listener(address, uid=uid, state=state)
                self.assertFalse(backend_listening(3394, 123, self.root, log=Mock()))
            self.proc_listener()
            self.assertTrue(backend_listening(3394, 123, self.root, log=Mock()))
            (self.root / 'net/tcp').unlink()
            self.proc_listener('00000000000000000000000001000000', name='tcp6')
            self.assertTrue(backend_listening(3394, 123, self.root, log=Mock()))
            self.proc_listener('00000000000000000000000000000000', name='tcp6')
            self.assertFalse(backend_listening(3394, 123, self.root, log=Mock()))

    def test_fd_permission_fallback_requires_same_uid_live_child(self):
        for state, uid in [('Z', os.geteuid()), ('X', os.geteuid()), ('S', os.geteuid() + 1)]:
            with self.subTest(state=state, uid=uid):
                self.proc_fixture(state=state, uid=uid)
                self.proc_listener(uid=uid)
                with self.denied_fd_inspection():
                    self.assertFalse(backend_listening(3394, 123, self.root, log=Mock()))
        self.proc_fixture(core_dumping=1)
        self.proc_listener()
        with self.denied_fd_inspection():
            self.assertFalse(backend_listening(3394, 123, self.root, log=Mock()))

    def test_fallback_rechecks_start_time_and_does_not_mask_unknown_proc_errors(self):
        self.proc_fixture()
        self.proc_listener()
        original = Path.read_text
        def replaced_process(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path == self.root / 'net/tcp':
                self.proc_fixture(start_time=901)
            return value
        with self.denied_fd_inspection(), patch.object(Path, 'read_text', replaced_process):
            self.assertFalse(backend_listening(3394, 123, self.root, log=Mock()))
        with patch.object(Path, 'iterdir', side_effect=OSError(errno.EIO, 'unknown procfs error')):
            with self.assertRaises(OSError):
                backend_listening(3394, 123, self.root, log=Mock())
        with self.denied_fd_inspection(), patch.object(Path, 'read_text', side_effect=PermissionError('status blocked')):
            with self.assertRaises(PermissionError):
                backend_listening(3394, 123, self.root, log=Mock())

    def test_fallback_is_only_used_on_permission_error_and_log_is_bounded(self):
        self.proc_fixture()
        self.proc_listener()
        # A readable but empty fd table is not evidence for the fallback.
        self.assertFalse(backend_listening(3394, 123, self.root, log=Mock()))
        logger = Mock()
        with self.denied_fd_inspection():
            for _ in range(10):
                self.assertTrue(backend_listening(3394, 123, self.root, log=logger))
        self.assertEqual(logger.call_count, 1)
        entry = json.loads(logger.call_args.args[0])
        self.assertEqual(entry['ownership'], 'same-uid-loopback-listener')
        self.assertEqual(entry['reason'], 'proc-fd-permission-denied')

    def nondumpable_child(self, address='127.0.0.1'):
        code = r'''import ctypes,json,os,socket,sys
libc=ctypes.CDLL(None,use_errno=True)
if libc.prctl(4,0,0,0,0)!=0: raise OSError(ctypes.get_errno(),'PR_SET_DUMPABLE failed')
s=socket.socket(socket.AF_INET6 if ':' in sys.argv[1] else socket.AF_INET)
s.bind((sys.argv[1],0));s.listen();s.settimeout(.05)
print(json.dumps({'pid':os.getpid(),'port':s.getsockname()[1]}),flush=True)
sys.stdin.readline()
try:
 peer,_=s.accept();peer.close();connected=True
except TimeoutError:
 connected=False
print(json.dumps({'unexpected_connection':connected}),flush=True)
s.close()
'''
        child = subprocess.Popen([sys.executable, '-u', '-c', code, address],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=2)
        self.addCleanup(cleanup)
        ready = json.loads(child.stdout.readline())
        try:
            list((Path('/proc') / str(child.pid) / 'fd').iterdir())
        except PermissionError:
            return child, ready['port']
        self.skipTest('This process can inspect non-dumpable child fds; kernel restriction not reproduced')

    def test_real_nondumpable_child_listens_without_any_readiness_connection(self):
        child, port = self.nondumpable_child()
        logger = Mock()
        self.assertIsNone(child.poll())
        for _ in range(3):
            self.assertTrue(backend_listening(port, child.pid, log=logger))
        self.assertEqual(logger.call_count, 1)
        output, errors = child.communicate('check\n', timeout=2)
        self.assertEqual(child.returncode, 0, errors)
        self.assertFalse(json.loads(output)['unexpected_connection'])
        self.assertFalse(backend_listening(port, child.pid, log=logger))

    def test_real_nondumpable_child_wildcard_listener_is_rejected(self):
        child, port = self.nondumpable_child('0.0.0.0')
        self.assertFalse(backend_listening(port, child.pid, log=Mock()))
        output, _ = child.communicate('check\n', timeout=2)
        self.assertFalse(json.loads(output)['unexpected_connection'])

    def test_watchdog_sends_only_from_owner_and_at_half_interval(self):
        path = str(self.root / 'notify.sock')
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listener:
            listener.bind(path)
            listener.settimeout(.05)
            watchdog = Watchdog({'NOTIFY_SOCKET': path, 'WATCHDOG_USEC': '10000000'}, self.clock)
            watchdog.ping()
            self.assertEqual(listener.recv(100), b'WATCHDOG=1')
            watchdog.ping()
            with self.assertRaises(TimeoutError):
                listener.recv(100)
            self.clock.return_value += 5
            watchdog.ping()
            self.assertEqual(listener.recv(100), b'WATCHDOG=1')
            other = Watchdog({'NOTIFY_SOCKET': path, 'WATCHDOG_USEC': '10000000',
                              'WATCHDOG_PID': str(os.getpid() + 1)}, self.clock)
            other.ping()
            with self.assertRaises(TimeoutError):
                listener.recv(100)


if __name__ == '__main__':
    unittest.main()
