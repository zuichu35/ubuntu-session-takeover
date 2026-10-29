#!/usr/bin/env python3
"""Add an authenticated, revocable desktop lease to GRD 50.2."""
from pathlib import Path
import difflib
import hashlib
import json
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'vendor/gnome-remote-desktop'
REVISION = '60423c896a54e3eacb65bd93167e91c1ce5e648c'
assert subprocess.check_output(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], text=True).strip() == REVISION
changes = {}


def edit(name, old, new):
    if name not in changes:
        changes[name] = subprocess.check_output(['git', '-C', str(SOURCE), 'show', f'HEAD:{name}'], text=True)
    assert changes[name].count(old) == 1, (name, old[:60])
    changes[name] = changes[name].replace(old, new)


code = r'''
/* The gate runs only after FreeRDP authentication. A socket lease is held
 * until BOTH network and Mutter input/capture have been torn down. */
static void
release_takeover (GrdSessionRdp *session_rdp)
{
  if (session_rdp->takeover_source)
    {
      g_source_destroy (session_rdp->takeover_source);
      g_clear_pointer (&session_rdp->takeover_source, g_source_unref);
    }
  if (session_rdp->takeover_connection)
    {
      g_io_stream_close (G_IO_STREAM (session_rdp->takeover_connection), NULL, NULL);
      g_clear_object (&session_rdp->takeover_connection);
    }
}

static gboolean
takeover_revoked (GSocket      *socket,
                  GIOCondition condition,
                  gpointer     user_data)
{
  GrdSessionRdp *session_rdp = user_data;
  /* Only the authenticated greeter lease may request an RDP handover. */
  if (g_getenv ("GRD_NATIVE_GREETER") && (condition & G_IO_IN))
    {
      char command[32];
      gssize length = g_socket_receive (socket, command, sizeof command, NULL, NULL);
      if (length == 9 && memcmp (command, "REDIRECT\n", 9) == 0)
        {
          session_rdp->is_view_only = TRUE; /* Freeze the old greeter before redirecting. */
          GrdSettings *settings = grd_context_get_settings (
            grd_session_get_context (GRD_SESSION (session_rdp)));
          g_autofree char *username = NULL, *password = NULL, *certificate = NULL;
          g_autoptr (GError) error = NULL;
          gboolean sent = FALSE;
          g_object_get (settings, "rdp-server-cert", &certificate, NULL);
          if (certificate && grd_settings_get_rdp_credentials (settings, &username, &password, &error))
            sent = grd_session_rdp_send_server_redirection (session_rdp,
              "3392", username, password, certificate);
          if (password) explicit_bzero (password, strlen (password));
          if (!sent)
            g_warning ("[Takeover] Native login handover failed: %s", error ? error->message : "redirection failed");
          /* Keep the transport until the client reconnects or its broker closes
           * the lease, giving the redirection PDU time to reach the client. */
          if (sent) return G_SOURCE_CONTINUE;
        }
    }
  /* Any other message, EOF, or broker failure revokes the lease. */
  g_message ("[Takeover] Lease revoked; closing remote session");
  grd_session_stop (GRD_SESSION (session_rdp));
  return G_SOURCE_REMOVE;
}

static gboolean
acquire_takeover (GrdSessionRdp *session_rdp)
{
  const char *path = g_getenv ("GRD_TAKEOVER_SOCKET");
  g_autoptr (GSocketClient) client = NULL;
  g_autoptr (GSocketAddress) address = NULL;
  g_autoptr (GSocketConnection) connection = NULL;
  g_autoptr (GError) error = NULL;
  gsize n_read = 0;
  char reply[8];
  g_autofree char *request = NULL;

  if (!path)
    return TRUE; /* Unmodified upstream behavior when feature is not enabled. */
  if (!g_path_is_absolute (path) || !*path)
    return FALSE;

  client = g_socket_client_new ();
  g_socket_client_set_timeout (client, 10);
  address = g_unix_socket_address_new (path);
  connection = g_socket_client_connect (client, G_SOCKET_CONNECTABLE (address),
                                        NULL, &error);
  if (!connection)
    goto failed;
  g_socket_set_timeout (g_socket_connection_get_socket (connection), 10);
  if (g_getenv ("GRD_NATIVE_TAKEOVER"))
    request = g_strdup_printf ("REMOTE %u %u\n",
      freerdp_settings_get_uint32 (session_rdp->peer->context->settings, FreeRDP_DesktopWidth),
      freerdp_settings_get_uint32 (session_rdp->peer->context->settings, FreeRDP_DesktopHeight));
  else
    request = g_strdup ("REMOTE\n");
  if (!g_output_stream_write_all (g_io_stream_get_output_stream (G_IO_STREAM (connection)),
                                  request, strlen (request), NULL, NULL, &error) ||
      !g_input_stream_read_all (g_io_stream_get_input_stream (G_IO_STREAM (connection)),
                                reply, sizeof reply, &n_read, NULL, &error) ||
      n_read != sizeof reply || memcmp (reply, "GRANTED\n", sizeof reply) != 0)
    goto failed;

  /* Bound the handshake only. A GSocket timeout also wakes its event source;
   * leaving it set would revoke every healthy connection after ten seconds. */
  g_socket_set_timeout (g_socket_connection_get_socket (connection), 0);
  session_rdp->takeover_connection = g_steal_pointer (&connection);
  g_message ("[Takeover] Authenticated client acquired exclusive lease");
  return TRUE;

failed:
  g_warning ("[Takeover] Refusing client: %s", error ? error->message : "lease denied");
  return FALSE;
}

gboolean
grd_session_rdp_resize_native (GrdSessionRdp *session_rdp,
                              uint32_t width,
                              uint32_t height)
{
  g_autofree char *request = NULL;
  g_autoptr (GError) error = NULL;
  if (!g_getenv ("GRD_NATIVE_TAKEOVER"))
    return FALSE;
  if (!session_rdp->takeover_connection)
    return TRUE;
  request = g_strdup_printf ("RESIZE %u %u\n", width, height);
  if (!g_output_stream_write_all (
        g_io_stream_get_output_stream (G_IO_STREAM (session_rdp->takeover_connection)),
        request, strlen (request), NULL, NULL, &error))
    g_warning ("[Takeover] Resize request failed: %s", error->message);
  return TRUE;
}

'''
edit('src/grd-session-rdp.c',
     '  if (grd_context_get_runtime_mode (context) == GRD_RUNTIME_MODE_HANDOVER)\n    {\n      freerdp_settings_set_string (rdp_settings, FreeRDP_Username, username);',
     '  if (grd_context_get_runtime_mode (context) == GRD_RUNTIME_MODE_HANDOVER ||\n      g_getenv ("GRD_NATIVE_LOOPBACK"))\n    {\n      freerdp_settings_set_string (rdp_settings, FreeRDP_Username, username);')
edit('src/grd-session-rdp.c',
     '  if (is_using_remote_login (session_rdp))\n    return TRUE;',
     """  /* RDSTLS authenticates against configured Username/Password in FreeRDP
   * before Logon. It has no NLA context to query. Keep the NLA/Kerberos
   * policy unchanged for every other negotiated protocol. */
  if (g_getenv ("GRD_NATIVE_LOOPBACK") &&
      freerdp_settings_get_uint32 (rdp_context->settings, FreeRDP_SelectedProtocol) == 0x00000004)
    return TRUE;
  if (is_using_remote_login (session_rdp))
    return TRUE;""")
file = 'src/grd-session-rdp.c'
edit(file, '  freerdp_settings_set_bool (rdp_settings, FreeRDP_NlaSecurity, TRUE);', '''  freerdp_settings_set_bool (rdp_settings, FreeRDP_NlaSecurity, TRUE);

  /* FreeRDP 3.32.x sends HYBRID_EX Early User Auth before the final
   * CredSSP credentials, stalling Windows App on macOS (FreeRDP #13549).
   * Keep TLS-protected NLA, but use HYBRID for this native backend until
   * the system library includes upstream cbb6a73. */
  if (g_getenv ("GRD_NATIVE_TAKEOVER"))
    freerdp_settings_set_bool (rdp_settings, FreeRDP_ExtSecurity, FALSE);''')
edit(file, '#include <gio/gio.h>', '#include <gio/gio.h>\n#include <gio/gunixsocketaddress.h>')
edit(file, '  GrdRdpServer *server;', '  GSocketConnection *takeover_connection;\n  GSource *takeover_source;\n\n  GrdRdpServer *server;')
edit(file, 'static void\nnotify_post_connected (gpointer user_data)', code + 'static void\nnotify_post_connected (gpointer user_data)')
edit(file, '  grd_rdp_session_metrics_notify_phase_completion (session_rdp->session_metrics,\n                                                   GRD_RDP_PHASE_POST_CONNECT);', '''  if (session_rdp->takeover_connection)
    {
      GSocket *socket = g_socket_connection_get_socket (session_rdp->takeover_connection);
      if (g_socket_condition_check (socket, G_IO_IN | G_IO_HUP | G_IO_ERR))
        {
          grd_session_stop (GRD_SESSION (session_rdp));
          return;
        }
      session_rdp->takeover_source = g_socket_create_source (socket,
                                                            G_IO_IN | G_IO_HUP | G_IO_ERR,
                                                            NULL);
      g_source_set_callback (session_rdp->takeover_source,
                             G_SOURCE_FUNC (takeover_revoked), session_rdp, NULL);
      g_source_attach (session_rdp->takeover_source, NULL);
    }

  grd_rdp_session_metrics_notify_phase_completion (session_rdp->session_metrics,
                                                   GRD_RDP_PHASE_POST_CONNECT);''')
edit(file, '  set_rdp_peer_flag (session_rdp, RDP_PEER_ACTIVATED);', '''  if (!acquire_takeover (session_rdp))
    return FALSE;

  set_rdp_peer_flag (session_rdp, RDP_PEER_ACTIVATED);''')
edit(file, '  g_assert (!session_rdp->cursor_renderer);', '  g_assert (!session_rdp->cursor_renderer);\n  release_takeover (session_rdp);')
edit(file, 'grd_session_rdp_init (GrdSessionRdp *session_rdp)\n{', '''grd_session_rdp_init (GrdSessionRdp *session_rdp)
{
  /* Base stopped signal is emitted after EI and Mutter session teardown. */
  g_signal_connect (session_rdp, "stopped", G_CALLBACK (release_takeover), NULL);''')
edit(file, '  if (session_rdp->screen_share_mode == GRD_RDP_SCREEN_SHARE_MODE_EXTEND)\n    {\n      rdp_peer_context->display_control =',
     '  if (session_rdp->screen_share_mode == GRD_RDP_SCREEN_SHARE_MODE_EXTEND ||\n      g_getenv ("GRD_NATIVE_TAKEOVER"))\n    {\n      rdp_peer_context->display_control =')
edit(file, '                                         get_max_monitor_count (session_rdp));',
     '                                         g_getenv ("GRD_NATIVE_TAKEOVER") ? 1 : get_max_monitor_count (session_rdp));')
edit('src/grd-session-rdp.h', 'GSocketConnection *grd_session_rdp_get_socket_connection (GrdSessionRdp *session_rdp);',
     'GSocketConnection *grd_session_rdp_get_socket_connection (GrdSessionRdp *session_rdp);\n\ngboolean grd_session_rdp_resize_native (GrdSessionRdp *session_rdp, uint32_t width, uint32_t height);')
edit('src/grd-rdp-dvc-display-control.c',
     '  grd_rdp_layout_manager_submit_new_monitor_config (layout_manager,\n                                                    monitor_config);',
     '''  if (grd_session_rdp_resize_native (session_rdp,
                                     monitor_config->desktop_width,
                                     monitor_config->desktop_height))
    {
      grd_rdp_monitor_config_free (monitor_config);
      return CHANNEL_RC_OK;
    }
  grd_rdp_layout_manager_submit_new_monitor_config (layout_manager,
                                                    monitor_config);''')
edit('src/grd-settings-headless.c', '"rdp-screen-share-mode", GRD_RDP_SCREEN_SHARE_MODE_EXTEND,', '''"rdp-screen-share-mode", g_getenv ("GRD_TAKEOVER_SOCKET") ?
                         GRD_RDP_SCREEN_SHARE_MODE_MIRROR_PRIMARY : GRD_RDP_SCREEN_SHARE_MODE_EXTEND,''')
edit('src/grd-rdp-pipewire-stream.c', '  if (egl_thread && !hwaccel_nvidia)\n    allowed_buffer_types |=',
     '  if (egl_thread && !hwaccel_nvidia && !g_getenv ("GRD_NATIVE_TAKEOVER"))\n    allowed_buffer_types |=')
# Gateway backends must never expose a second unaudited public listener.
edit('src/grd-utils.c', '  if (!negotiate_port)\n    {', """  if (g_strcmp0 (g_getenv ("GRD_NATIVE_LOOPBACK"), "1") == 0)
    {
      g_autoptr (GInetAddress) address = g_inet_address_new_loopback (G_SOCKET_FAMILY_IPV4);
      g_autoptr (GSocketAddress) socket_address = g_inet_socket_address_new (address, port);
      is_bound = g_socket_listener_add_address (server, socket_address, G_SOCKET_TYPE_STREAM,
        G_SOCKET_PROTOCOL_TCP, NULL, NULL, error);
      goto out;
    }
  if (!negotiate_port)
    {""")
manifest = ROOT / 'build/grd-patch-hashes.json'
previous = json.loads(manifest.read_text()) if manifest.exists() else {}
digest = lambda text: hashlib.sha256(text.encode()).hexdigest()

patch = []
for name, modified in changes.items():
    original = subprocess.check_output(['git', '-C', str(SOURCE), 'show', f'HEAD:{name}'], text=True)
    patch.extend(difflib.unified_diff(original.splitlines(True), modified.splitlines(True), fromfile='a/'+name, tofile='b/'+name))
    current = SOURCE / name
    # Only overwrite a pristine file or this tool's previous output.
    if current.read_text() not in (original, modified) and digest(current.read_text()) != previous.get(name):
        raise SystemExit(f'Unrecognized existing edits: {name}')
    current.write_text(modified)
(ROOT / 'patches/grd-takeover.patch').write_text(''.join(patch))

manifest.write_text(json.dumps({name:digest(text) for name,text in changes.items()}, indent=2) + '\n')
