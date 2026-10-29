"""Exercise the production C identity functions with controlled NSS/logind data."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NativeGreeterIdentityTests(unittest.TestCase):
    def test_c_authorization_boundary(self):
        source = (ROOT / 'takeover/native-privacy.c').read_text()
        functions = source[source.index('static GVariant *\nsession_properties ('):
                           source.index('/* Fault injection is restricted')]
        fixture = r'''
#include <gio/gio.h>
#include <pwd.h>
#include <grp.h>
#include <unistd.h>
static GDBusConnection *system_bus = (GDBusConnection *) 1;
static gboolean test_mode, greeter_mode;
static char *greeter_session_path;
static uid_t current_uid = 60580;
static gboolean have_account = TRUE, have_group = TRUE;
static struct passwd account = { .pw_gid = 121 };
static struct group group = { .gr_gid = 121 };
static uid_t fake_getuid (void) { return current_uid; }
static struct passwd *fake_getpwuid (uid_t uid) { return have_account ? &account : NULL; }
static struct group *fake_getgrnam (const char *name) { return have_group && !strcmp(name, "gdm") ? &group : NULL; }
typedef struct {
  const char *path, *seat, *klass, *type;
  guint uid, property_uid;
  gboolean remote, omit_remote;
  const char *state;
} Session;
static Session sessions[3];
static guint count;
static guint failed_lists, list_calls, sleeps;
static void fake_sleep (gulong usec) { g_assert_cmpuint (usec, ==, 250000); sleeps++; }
static GVariant *fake_call (GDBusConnection *bus, const char *name, const char *path,
                           const char *interface, const char *method, GVariant *parameters,
                           const GVariantType *reply_type, GDBusCallFlags flags, gint timeout,
                           GCancellable *cancel, GError **error)
{
  GVariantBuilder builder;
  if (parameters) g_variant_unref (g_variant_ref_sink (parameters));
  if (!strcmp (method, "ListSessions"))
    {
      list_calls++;
      if (failed_lists) { failed_lists--; return NULL; }
      g_variant_builder_init (&builder, G_VARIANT_TYPE ("a(susso)"));
      for (guint i = 0; i < count; i++)
        g_variant_builder_add (&builder, "(susso)", "c1", sessions[i].uid,
                               "untrusted-name", sessions[i].seat, sessions[i].path);
      return g_variant_ref_sink (g_variant_new ("(@a(susso))", g_variant_builder_end (&builder)));
    }
  for (guint i = 0; i < count; i++)
    {
      Session *s = &sessions[i];
      if (strcmp (path, s->path)) continue;
      g_variant_builder_init (&builder, G_VARIANT_TYPE ("a{sv}"));
      g_variant_builder_add (&builder, "{sv}", "User", g_variant_new ("(uo)", s->property_uid, "/user"));
      g_variant_builder_add (&builder, "{sv}", "Seat", g_variant_new ("(so)", s->seat, "/seat"));
      g_variant_builder_add (&builder, "{sv}", "Type", g_variant_new_string (s->type));
      g_variant_builder_add (&builder, "{sv}", "Class", g_variant_new_string (s->klass));
      g_variant_builder_add (&builder, "{sv}", "Active", g_variant_new_boolean (FALSE));
      if (s->state)
        g_variant_builder_add (&builder, "{sv}", "State", g_variant_new_string (s->state));
      if (!s->omit_remote)
        g_variant_builder_add (&builder, "{sv}", "Remote", g_variant_new_boolean (s->remote));
      return g_variant_ref_sink (g_variant_new ("(@a{sv})", g_variant_builder_end (&builder)));
    }
  return NULL;
}
#define getuid fake_getuid
#define getpwuid fake_getpwuid
#define getgrnam fake_getgrnam
#define g_dbus_connection_call_sync fake_call
#define g_usleep fake_sleep
'''
        checks = r'''
static void reset (void)
{
  current_uid = 60580; account.pw_gid = 121; group.gr_gid = 121;
  have_account = have_group = TRUE; test_mode = greeter_mode = FALSE;
  system_bus = (GDBusConnection *) 1;
  count = 1;
  failed_lists = list_calls = sleeps = 0;
  sessions[0] = (Session) { "/sessions/s1", "seat0", "greeter", "wayland", 60580, 60580, FALSE, FALSE, NULL };
}
static void deny (void) { g_autofree char *path = find_greeter_session (); g_assert_null (path); }
int main (void)
{
  reset ();
  g_autofree char *path = find_greeter_session ();
  g_assert_cmpstr (path, ==, "/sessions/s1"); /* Active=false is valid during startup. */
  greeter_mode = TRUE; greeter_session_path = path;
  g_assert_true (validate_session (path));
  g_assert_false (validate_session ("/sessions/other"));
  account.pw_gid = 1000; g_assert_false (validate_session (path));
  account.pw_gid = 121; have_account = FALSE; g_assert_false (validate_session (path));
  have_account = TRUE; have_group = FALSE; g_assert_false (validate_session (path));
  have_group = TRUE; sessions[0].state = "closing"; g_assert_false (validate_session (path)); deny ();
  sessions[0].state = "online"; g_assert_true (validate_session (path));
  sessions[0].remote = TRUE; g_assert_false (validate_session (path)); deny ();
  reset (); sessions[0].seat = "seat1"; deny ();
  reset (); sessions[0].type = "x11"; deny ();
  reset (); sessions[0].klass = "user"; deny ();
  reset (); sessions[0].property_uid = 1000; deny ();
  reset (); sessions[0].uid = 1000; deny ();
  reset (); sessions[0].omit_remote = TRUE; deny ();
  reset (); sessions[0].state = "closing"; deny ();
  reset (); have_account = FALSE; deny ();
  reset (); have_group = FALSE; deny ();
  reset (); account.pw_gid = 1000; deny (); /* Supplementary membership cannot authorize. */
  reset (); count = 2; sessions[1] = sessions[0]; sessions[1].path = "/sessions/s2"; deny ();
  reset (); count = 0; deny ();
  reset (); system_bus = NULL; deny ();
  reset (); current_uid = sessions[0].uid = sessions[0].property_uid = 60582;
  g_autofree char *changed = find_greeter_session ();
  g_assert_cmpstr (changed, ==, "/sessions/s1"); /* Dynamic UID need not equal legacy gdm. */
  reset (); sessions[0].klass = "user";
  g_assert_true (validate_session (sessions[0].path));
  sessions[0].remote = TRUE; g_assert_false (validate_session (sessions[0].path));
  test_mode = TRUE; g_assert_true (validate_session ("/isolated/test"));
  reset (); failed_lists = 2;
  g_autofree char *retried = find_greeter_session_with_retry ();
  g_assert_cmpstr (retried, ==, "/sessions/s1");
  g_assert_cmpuint (list_calls, ==, 3); g_assert_cmpuint (sleeps, ==, 2);
  reset (); failed_lists = 5;
  g_assert_null (find_greeter_session_with_retry ());
  g_assert_cmpuint (list_calls, ==, 3); g_assert_cmpuint (sleeps, ==, 2);
  reset (); account.pw_gid = 1000;
  g_assert_null (find_greeter_session_with_retry ());
  g_assert_cmpuint (list_calls, ==, 0); g_assert_cmpuint (sleeps, ==, 2);
  g_print ("native C NSS/logind authorization checks passed\n");
  return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix='native-greeter-identity-') as directory:
            path = Path(directory)
            (path / 'identity.c').write_text(fixture + functions + checks)
            flags = subprocess.check_output(['pkg-config', '--cflags', '--libs', 'gio-2.0'], text=True).split()
            subprocess.run(['cc', '-Wall', '-Wextra', '-Wno-unused-parameter', '-Werror',
                            str(path / 'identity.c'), '-o', str(path / 'identity'), *flags], check=True)
            result = subprocess.run([str(path / 'identity')], capture_output=True, text=True, check=True)
            self.assertIn('authorization checks passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
