import errno
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock

from takeover.startup_guard import (
    LocalReclaimRejected, UnsafePrivacyState, finish_local_reclaim,
    request_local_reclaim, status_snapshot, wait_startup_ready,
)


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class StartupGuardTests(unittest.TestCase):
    def test_startup_confirmation_prepares_once_and_never_unlocks(self):
        clock = Clock()
        states = iter(('boot-guard', 'startup-locking', 'ready-guarded'))
        prepare, heartbeat = Mock(), Mock()
        self.assertEqual(wait_startup_ready(lambda: next(states), prepare,
            clock=clock, sleep=clock.sleep, heartbeat=heartbeat, log=lambda _: None), 'ready-guarded')
        prepare.assert_called_once_with()
        self.assertEqual(heartbeat.call_count, 3)

    def test_supervisor_waits_passively_after_failure_until_local_recovery(self):
        clock = Clock()
        def read():
            return 'startup-locking' if clock.now < 30 else 'startup-failed' if clock.now < 40 else 'local'
        prepare, messages = Mock(), []
        result = wait_startup_ready(read, prepare, wait_for_recovery=True,
            read_status=lambda: {'state': read(), 'reason': 'lock-confirmation-timeout'},
            clock=clock, sleep=clock.sleep, log=lambda message: messages.append(json.loads(message)))
        self.assertEqual(result, 'local')
        prepare.assert_not_called()
        self.assertEqual(clock.now, 40)
        self.assertEqual(clock.sleeps[-10:], [1] * 10)
        self.assertEqual(sum(m['event'] == 'native-startup-wait-expired' for m in messages), 1)

    def test_bounded_helper_exits_without_resetting_failed_guard(self):
        clock, prepare = Clock(), Mock()
        self.assertIsNone(wait_startup_ready(lambda: 'startup-locking', prepare, timeout=2,
            clock=clock, sleep=clock.sleep, log=lambda _: None))
        self.assertEqual(clock.now, 2)
        prepare.assert_not_called()
        self.assertIsNone(wait_startup_ready(lambda: 'startup-failed', prepare,
            clock=clock, sleep=clock.sleep, log=lambda _: None))
        self.assertEqual(clock.now, 2)

    def test_stalled_boot_guard_does_not_repeat_prepare_during_passive_recovery(self):
        clock, prepare = Clock(), Mock()
        result = wait_startup_ready(lambda: 'boot-guard' if clock.now < 4 else 'ready-guarded',
            prepare, timeout=1, wait_for_recovery=True,
            clock=clock, sleep=clock.sleep, log=lambda _: None)
        self.assertEqual(result, 'ready-guarded')
        prepare.assert_called_once_with()
        self.assertGreaterEqual(clock.sleeps.count(1), 3)

    def test_remote_blanking_and_ordinary_locking_are_never_adopted(self):
        for state in ('remote', 'blanking', 'locking', 'unknown'):
            with self.subTest(state=state):
                prepare, sleep = Mock(), Mock()
                with self.assertRaises(UnsafePrivacyState):
                    wait_startup_ready(lambda: state, prepare, sleep=sleep)
                prepare.assert_not_called()
                sleep.assert_not_called()

    def test_prepare_race_accepts_only_another_valid_startup_transition(self):
        clock = Clock()
        states = iter(('boot-guard', 'startup-locking', 'ready-guarded'))
        prepare = Mock(side_effect=RuntimeError('another helper prepared it'))
        result = wait_startup_ready(lambda: next(states), prepare,
            clock=clock, sleep=clock.sleep, log=lambda _: None)
        self.assertEqual(result, 'ready-guarded')
        states = iter(('boot-guard', 'remote'))
        with self.assertRaisesRegex(RuntimeError, 'another helper'):
            wait_startup_ready(lambda: next(states), prepare, log=lambda _: None)

    def test_stopping_during_guard_wait_propagates_immediately(self):
        with self.assertRaises(KeyboardInterrupt):
            wait_startup_ready(lambda: 'startup-failed', Mock(), wait_for_recovery=True,
                               sleep=Mock(side_effect=KeyboardInterrupt), log=lambda _: None)

    def test_status_logs_only_documented_diagnostic_fields(self):
        self.assertEqual(status_snapshot(lambda: {'state': 'startup-failed', 'secret': 'omit',
                          'local-authenticated': False}),
                         {'state': 'startup-failed', 'local-authenticated': False})


class LocalReclaimTests(unittest.TestCase):
    def test_absent_guard_requires_no_reclaim_or_status_request(self):
        status, request = Mock(), Mock()
        finish_local_reclaim('/session', status, lambda: False, request=request)
        status.assert_not_called()
        request.assert_not_called()

    def test_local_state_without_authentication_proof_is_refused(self):
        for status in ({'state': 'local', 'local-authenticated': False},
                       {'state': 'local', 'local-authenticated': 'true'},
                       {'state': 'startup-failed', 'local-authenticated': True}):
            with self.subTest(status=status):
                request = Mock()
                with self.assertRaises(LocalReclaimRejected):
                    finish_local_reclaim('/session', lambda: status, lambda: True, request=request)
                request.assert_not_called()

    def test_transport_recovery_rechecks_status_without_backend_restart(self):
        request = Mock(side_effect=[FileNotFoundError(errno.ENOENT, 'gateway absent'), None])
        status = Mock(return_value={'state': 'local', 'local-authenticated': True})
        heartbeat, sleep = Mock(), Mock()
        finish_local_reclaim('/session', status, lambda: True, request=request,
                             heartbeat=heartbeat, sleep=sleep, log=lambda _: None)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(status.call_count, 2)
        sleep.assert_called_once_with(.5)
        self.assertEqual(heartbeat.call_count, 2)

    def test_explicit_root_rejection_is_not_retried(self):
        request = Mock(side_effect=LocalReclaimRejected('root refused'))
        sleep = Mock()
        with self.assertRaises(LocalReclaimRejected):
            finish_local_reclaim('/session', lambda: {'state': 'local', 'local-authenticated': True},
                                 lambda: True, request=request, sleep=sleep)
        request.assert_called_once_with('/session')
        sleep.assert_not_called()

    def test_real_unix_protocol_sends_no_client_claim_of_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'control.sock')
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(path)
                server.listen()
                received = []
                def respond():
                    with server.accept()[0] as client:
                        received.append(json.loads(client.makefile('rb').readline()))
                        client.sendall(b'RECL')
                        client.sendall(b'AIMED\n')
                worker = threading.Thread(target=respond)
                worker.start()
                request_local_reclaim('/native-session', path=path)
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
        self.assertEqual(received, [{'command': 'local-reclaim', 'role': 'desktop',
                                     'session': '/native-session'}])

    def test_gateway_retryable_rejection_is_strict_and_never_authorizes(self):
        for response, error_type in ((b'{"error":"unavailable","retryable":true}\n', BlockingIOError),
                                     (b'{"error":"refused","retryable":false}\n', LocalReclaimRejected),
                                     (b'{"retryable":"true"}\n', LocalReclaimRejected),
                                     (b'not-json\n', LocalReclaimRejected)):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / 'control.sock')
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(path)
                    server.listen()
                    def respond():
                        with server.accept()[0] as client:
                            client.makefile('rb').readline()
                            client.sendall(response)
                    worker = threading.Thread(target=respond)
                    worker.start()
                    with self.assertRaises(error_type):
                        request_local_reclaim('/native-session', path=path)
                    worker.join(timeout=2)
                    self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()
