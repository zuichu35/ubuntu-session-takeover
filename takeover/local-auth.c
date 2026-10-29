/* SPDX-License-Identifier: GPL-2.0-or-later
 * One-shot system-password verifier. Password arrives only on a private pipe.
 * No setuid, session creation, credential storage, or authentication bypass.
 */
#define _GNU_SOURCE
#include <security/pam_appl.h>
#include <pwd.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/resource.h>
#include <sys/prctl.h>

static char secret[1024];
static int answered;

static int converse(int count, const struct pam_message **messages,
                    struct pam_response **out, void *data)
{
    (void)data;
    if (count < 1 || count > 16) return PAM_CONV_ERR;
    struct pam_response *responses = calloc(count, sizeof(*responses));
    if (!responses) return PAM_BUF_ERR;
    for (int i = 0; i < count; i++) {
        switch (messages[i]->msg_style) {
        case PAM_PROMPT_ECHO_OFF:
            if (answered++) goto fail;
            responses[i].resp = strdup(secret);
            if (!responses[i].resp) goto fail;
            break;
        case PAM_TEXT_INFO:
        case PAM_ERROR_MSG:
            break;
        default:
            goto fail;
        }
    }
    *out = responses;
    return PAM_SUCCESS;
fail:
    for (int i = 0; i < count; i++) {
        if (responses[i].resp) {
            explicit_bzero(responses[i].resp, strlen(responses[i].resp));
            free(responses[i].resp);
        }
    }
    free(responses);
    return PAM_CONV_ERR;
}

int main(void)
{
    struct rlimit limit = {0, 0};
    setrlimit(RLIMIT_CORE, &limit);
    prctl(PR_SET_DUMPABLE, 0);
    alarm(30);
    /* The native compositor and helper must run as the same ordinary user. */
    if (!getuid() || getuid() != geteuid()) return 2;
    struct passwd *user = getpwuid(getuid());
    if (!user) return 2;
    size_t used = 0;
    ssize_t got;
    while ((got = read(STDIN_FILENO, secret + used, sizeof(secret) - 1 - used)) > 0) {
        used += got;
        if (used == sizeof(secret) - 1) {
            explicit_bzero(secret, sizeof(secret));
            return 2;
        }
    }
    if (got < 0 || !used || memchr(secret, 0, used)) {
        explicit_bzero(secret, sizeof(secret));
        return 2;
    }
    secret[used] = 0;
    struct pam_conv conversation = {converse, NULL};
    pam_handle_t *handle = NULL;
    int status = pam_start("gdm-password", user->pw_name, &conversation, &handle);
    if (status == PAM_SUCCESS)
        status = pam_authenticate(handle, PAM_DISALLOW_NULL_AUTHTOK);
    explicit_bzero(secret, sizeof(secret));
    if (status == PAM_SUCCESS) status = pam_acct_mgmt(handle, 0);
    if (handle) pam_end(handle, status);
    return status == PAM_SUCCESS && answered ? 0 : 1;
}
