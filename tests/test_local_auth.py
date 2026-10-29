"""Compile the production helper against a fake PAM to check its boundary."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PAM = r'''
#include <security/pam_appl.h>
#include <stdlib.h>
#include <string.h>
static const struct pam_conv *conversation;
int pam_start(const char *service, const char *user, const struct pam_conv *conv, pam_handle_t **handle) {
  if (strcmp(service, "gdm-password") || !user || !*user) return PAM_SYSTEM_ERR;
  conversation = conv; *handle = (void*)1; return PAM_SUCCESS;
}
int pam_authenticate(pam_handle_t *handle, int flags) {
  if (!(flags & PAM_DISALLOW_NULL_AUTHTOK)) return PAM_SYSTEM_ERR;
  if (getenv("TEST_PAM_NO_PROMPT")) return PAM_SUCCESS;
  struct pam_message message = {getenv("TEST_PAM_ECHO") ? PAM_PROMPT_ECHO_ON : PAM_PROMPT_ECHO_OFF, "Password:"};
  const struct pam_message *pointer = &message;
  struct pam_response *response = NULL;
  int result = conversation->conv(1, &pointer, &response, NULL);
  if (result != PAM_SUCCESS) return result;
  int correct = response && response->resp && !strcmp(response->resp, "test-password-only");
  if (response) { free(response->resp); free(response); }
  if (getenv("TEST_PAM_REPEAT")) return conversation->conv(1, &pointer, &response, NULL);
  return correct ? PAM_SUCCESS : PAM_AUTH_ERR;
}
int pam_acct_mgmt(pam_handle_t *handle, int flags) {return getenv("TEST_PAM_EXPIRED") ? PAM_ACCT_EXPIRED : PAM_SUCCESS;}
int pam_end(pam_handle_t *handle, int status) {return PAM_SUCCESS;}
'''


class LocalAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='local-auth-test-')
        directory = Path(cls.directory.name)
        (directory / 'pam.c').write_text(PAM)
        cls.helper = directory / 'helper'
        subprocess.run(['cc', '-O2',
                        str(ROOT/'takeover/local-auth.c'), str(directory/'pam.c'),
                        '-o', str(cls.helper)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def run_helper(self, password, expected, **extra):
        result = subprocess.run([str(self.helper)], input=password, capture_output=True,
                                env=dict(os.environ, **extra), timeout=5)
        self.assertEqual(result.returncode, expected)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'')

    def test_password_and_account_must_both_pass(self):
        self.run_helper(b'test-password-only', 0)
        self.run_helper(b'wrong', 1)
        self.run_helper(b'test-password-only', 1, TEST_PAM_EXPIRED='1')

    def test_no_password_or_unsupported_prompt_never_passes(self):
        self.run_helper(b'test-password-only', 1, TEST_PAM_NO_PROMPT='1')
        self.run_helper(b'test-password-only', 1, TEST_PAM_ECHO='1')
        self.run_helper(b'test-password-only', 1, TEST_PAM_REPEAT='1')

    def test_invalid_input_is_rejected_without_authentication(self):
        self.run_helper(b'', 2)
        self.run_helper(b'test-password-only\0trailing', 2)
        self.run_helper(b'a' * 1024, 2)


if __name__ == '__main__':
    unittest.main()
