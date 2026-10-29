import unittest
from types import SimpleNamespace
from unittest.mock import patch

from takeover.greeter_identity import (
    find_native_greeter_session, is_gdm_group_member, is_native_greeter_session,
)


def greeter_properties(uid=60580, **changes):
    properties = {'User': (uid, '/user'), 'Seat': ('seat0', '/seat'),
                  'Type': 'wayland', 'Class': 'greeter', 'Remote': False,
                  'State': 'active'}
    properties.update(changes)
    return properties


class GreeterIdentityTests(unittest.TestCase):
    def test_nss_primary_group_is_resolved_each_time_without_trusting_name(self):
        with patch('takeover.greeter_identity.grp.getgrnam', return_value=SimpleNamespace(gr_gid=121)), \
             patch('takeover.greeter_identity.pwd.getpwuid', side_effect=[
                 SimpleNamespace(pw_name='transient-name', pw_gid=121),
                 SimpleNamespace(pw_name='gdm-greeter', pw_gid=1000),
                 KeyError('expired dynamic account')]):
            self.assertTrue(is_gdm_group_member(60580))
            self.assertFalse(is_gdm_group_member(60580))
            self.assertFalse(is_gdm_group_member(60580))

    def test_group_membership_does_not_replace_logind_identity(self):
        base = greeter_properties()
        self.assertTrue(is_native_greeter_session(60580, base, lambda uid: True))
        self.assertFalse(is_native_greeter_session(60580, base, lambda uid: False))
        for change in ({'User': (60581, '/other')}, {'Seat': ('seat1', '/other')},
                       {'Type': 'x11'}, {'Class': 'user'}, {'Remote': True},
                       {'Remote': None}, {'State': 'closing'}):
            with self.subTest(change=change):
                self.assertFalse(is_native_greeter_session(60580, dict(base, **change), lambda uid: True))

    def test_real_session_identification_ignores_username_and_rejects_ambiguity(self):
        sessions = [('c3', 60580, 'not-a-prefix', 'seat0', '/greeter')]
        properties = {'/greeter': greeter_properties()}
        find = lambda: find_native_greeter_session(60580, lambda: sessions,
            properties.__getitem__, lambda uid: True)
        self.assertEqual(find(), '/greeter')
        sessions.append(('c4', 60580, 'gdm-greeter-2', 'seat0', '/other'))
        properties['/other'] = greeter_properties()
        self.assertIsNone(find())

    def test_disappearing_or_unavailable_logind_never_confirms_role(self):
        sessions = [('c3', 60580, 'gdm-greeter', 'seat0', '/gone')]
        self.assertIsNone(find_native_greeter_session(60580, lambda: sessions,
            {}.__getitem__, lambda uid: True))
        with patch('takeover.greeter_identity.logind_sessions', side_effect=RuntimeError('unavailable')):
            self.assertIsNone(find_native_greeter_session(60580, group_check=lambda uid: True))

    def test_regular_account_never_needs_logind_or_greeter_configuration(self):
        with patch('takeover.greeter_identity.logind_sessions') as lookup:
            self.assertIsNone(find_native_greeter_session(1000, group_check=lambda uid: False))
            lookup.assert_not_called()


if __name__ == '__main__':
    unittest.main()
