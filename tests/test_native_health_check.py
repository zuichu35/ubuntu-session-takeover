import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('native_health_check',
    Path(__file__).resolve().parents[1] / 'scripts/check-native-health.py')
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


class HealthCheckTests(unittest.TestCase):
    def test_linux_socket_table_distinguishes_listeners_and_loopback(self):
        ipv4 = '''  sl  local_address rem_address st
  0: 00000000:0D40 00000000:0000 0A
  1: 0100007F:0D41 00000000:0000 0A
  2: 0100007F:0D41 0100007F:8020 01
  3: 0100007F:0016 00000000:0000 0A
'''
        self.assertEqual(health.parse_listeners(ipv4), [
            {'address': '0.0.0.0', 'port': 3392, 'loopback': False},
            {'address': '127.0.0.1', 'port': 3393, 'loopback': True}])
        ipv6 = '''  sl  local_address rem_address st
  0: 00000000000000000000000001000000:0D41 00000000000000000000000000000000:0000 0A
  1: 00000000000000000000000000000000:0D41 00000000000000000000000000000000:0000 0A
'''
        self.assertEqual(health.parse_listeners(ipv6, ipv6=True), [
            {'address': '::1', 'port': 3393, 'loopback': True},
            {'address': '::', 'port': 3393, 'loopback': False}])

    def test_tls_probe_requires_both_idle_broker_and_no_connections(self):
        with patch.object(health, 'broker_status', return_value={'native_state': 'local', 'remote': True}), \
             patch.object(health, 'command') as command:
            with self.assertRaisesRegex(RuntimeError, 'not locally idle'):
                health.assert_probe_idle()
            command.assert_not_called()
        with patch.object(health, 'broker_status', return_value={'native_state': 'local', 'remote': False}), \
             patch.object(health, 'command', return_value='0 0 127.0.0.1:3392 127.0.0.1:48200'):
            with self.assertRaisesRegex(RuntimeError, 'connection already exists'):
                health.assert_probe_idle()

    def test_receive_exact_handles_fragmentation_and_eof(self):
        class Fragmented:
            def __init__(self, chunks):
                self.chunks = iter(chunks)

            def recv(self, size):
                return next(self.chunks, b'')

        self.assertEqual(health.receive_exact(Fragmented([b'\x03', b'\x00\x00', b'\x13']), 4),
                         b'\x03\x00\x00\x13')
        with self.assertRaisesRegex(RuntimeError, 'Unexpected EOF'):
            health.receive_exact(Fragmented([b'\x03']), 4)


if __name__ == '__main__':
    unittest.main()
