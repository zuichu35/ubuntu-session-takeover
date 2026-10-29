"""An authenticated RDP lease is not evidence that its X11 client is mapped."""
import subprocess
import unittest
from unittest.mock import Mock

from integration import wait_for_client_window


class ClientWindowReadiness(unittest.TestCase):
    def test_delayed_mapping_waits_for_a_real_window(self):
        process = Mock(returncode=None)
        process.poll.return_value = None
        query = Mock(side_effect=[subprocess.CompletedProcess([], 1, ''),
                                 subprocess.CompletedProcess([], 0, '123\n')])
        sleep = Mock()
        self.assertEqual(wait_for_client_window(process, {'DISPLAY': ':private'}, 'client',
                         run=query, sleep=sleep, clock=lambda: 0), '123')
        self.assertEqual(query.call_count, 2)
        sleep.assert_called_once_with(.1)

    def test_client_failure_is_not_hidden_by_a_stale_window(self):
        process = Mock(returncode=7)
        process.poll.return_value = 7
        query = Mock()
        with self.assertRaisesRegex(AssertionError, 'exited with status 7'):
            wait_for_client_window(process, {}, 'client', run=query)
        query.assert_not_called()

    def test_live_client_without_window_times_out(self):
        process = Mock()
        process.poll.return_value = None
        query = Mock(return_value=subprocess.CompletedProcess([], 1, ''))
        with self.assertRaisesRegex(AssertionError, 'did not map its window'):
            wait_for_client_window(process, {}, 'client', seconds=1, run=query,
                                   sleep=Mock(), clock=Mock(side_effect=[0, 0, 2]))
