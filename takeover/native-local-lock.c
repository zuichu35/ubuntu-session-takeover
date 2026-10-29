/* SPDX-License-Identifier: GPL-2.0-or-later
 * A physical-output-only authentication surface. Never a scene actor, never
 * captured by PipeWire. Only fresh raw libinput keys enter its private keymap.
 */
#include "config.h"
#include "backends/meta-native-local-lock.h"
#include <cairo.h>
#include <pango/pangocairo.h>
#include <linux/input-event-codes.h>
#include <string.h>
#include <sys/mman.h>

static ClutterActor *local_stage;
static struct xkb_state *key_state;
static void (*on_verified) (void);
static gboolean running, busy, dirty, isolated_test;
static gboolean recovery_mode;
static guint generation, ticker;
static gint64 retry_after;
static char password[1024];
static const char *status_text;
static CoglTexture *texture;
static GSubprocess *authentication;
static guint held[KEY_CNT];

static void redraw (void)
{
  dirty = TRUE;
  if (local_stage) clutter_actor_queue_redraw (local_stage);
}

static gboolean tick (gpointer unused)
{
  if (!running) { ticker = 0; return G_SOURCE_REMOVE; }
  redraw ();
  return G_SOURCE_CONTINUE;
}

static void clear_password (void) { explicit_bzero (password, sizeof password); }

void meta_native_local_lock_end (void)
{
  running = FALSE;
  generation++;
  if (authentication) g_subprocess_force_exit (authentication);
  g_clear_object (&authentication);
  if (ticker) { g_source_remove (ticker); ticker = 0; }
  g_clear_object (&texture);
  if (key_state) { xkb_state_unref (key_state); key_state = NULL; }
  memset (held, 0, sizeof held);
  clear_password ();
  local_stage = NULL;
  on_verified = NULL;
  busy = FALSE;
}

void meta_native_local_lock_begin (ClutterActor *stage, struct xkb_keymap *keymap,
                                   xkb_layout_index_t group, gboolean caps_lock,
                                   gboolean num_lock, gboolean test_mode,
                                   void (*verified) (void))
{
  meta_native_local_lock_end ();
  local_stage = stage;
  key_state = xkb_state_new (keymap);
  xkb_mod_mask_t locked = 0;
  xkb_mod_index_t caps = xkb_keymap_mod_get_index (keymap, XKB_MOD_NAME_CAPS);
  xkb_mod_index_t num = xkb_keymap_mod_get_index (keymap, XKB_MOD_NAME_NUM);
  if (caps_lock && caps != XKB_MOD_INVALID) locked |= 1u << caps;
  if (num_lock && num != XKB_MOD_INVALID) locked |= 1u << num;
  xkb_state_update_mask (key_state, 0, 0, locked, 0, 0, group);
  on_verified = verified;
  isolated_test = test_mode;
  running = TRUE;
  recovery_mode = FALSE;
  retry_after = 0;
  status_text = "输入本机密码，按 Enter 接回桌面";
  (void) mlock (password, sizeof password);
  ticker = g_timeout_add_seconds (10, tick, NULL);
  redraw ();
}

void meta_native_local_lock_set_recovery (gboolean recovery)
{
  if (recovery_mode == recovery) return;
  recovery_mode = recovery;
  redraw ();
}

typedef struct { guint generation; } AuthAttempt;

static void auth_done (GObject *source, GAsyncResult *result, gpointer data)
{
  AuthAttempt *attempt = data;
  gboolean ok = g_subprocess_wait_finish (G_SUBPROCESS (source), result, NULL) &&
                g_subprocess_get_successful (G_SUBPROCESS (source));
  if (running && attempt->generation == generation &&
      authentication == G_SUBPROCESS (source))
    {
      g_clear_object (&authentication);
      busy = FALSE;
      if (ok)
        {
          status_text = "验证成功，正在接回桌面…";
          redraw ();
          if (on_verified) on_verified ();
        }
      else
        {
          retry_after = g_get_monotonic_time () + 2 * G_USEC_PER_SEC;
          status_text = "密码错误或认证不可用，请稍后重试";
          redraw ();
        }
    }
  g_free (attempt);
}

static void submit (void)
{
  const char *helper = g_getenv ("MUTTER_NATIVE_LOCAL_AUTH_HELPER");
  if (busy || !*password || g_get_monotonic_time () < retry_after) return;
  if (isolated_test && g_getenv ("MUTTER_NATIVE_TEST_AUTH_HELPER"))
    helper = g_getenv ("MUTTER_NATIVE_TEST_AUTH_HELPER");
  if (!helper || !g_path_is_absolute (helper))
    {
      clear_password ();
      status_text = "认证服务未就绪，请从远程端断开连接";
      redraw ();
      return;
    }
  authentication = g_subprocess_new (G_SUBPROCESS_FLAGS_STDIN_PIPE |
                                      G_SUBPROCESS_FLAGS_STDOUT_SILENCE |
                                      G_SUBPROCESS_FLAGS_STDERR_SILENCE,
                                      NULL, helper, NULL);
  if (!authentication)
    {
      clear_password ();
      status_text = "认证服务无法启动，请从远程端断开连接";
      redraw ();
      return;
    }
  /* At most 1023 bytes: fits in the private pipe without waiting on PAM. */
  GOutputStream *input = g_subprocess_get_stdin_pipe (authentication);
  gboolean sent = g_output_stream_write_all (input, password, strlen (password), NULL, NULL, NULL);
  g_output_stream_close (input, NULL, NULL);
  clear_password ();
  if (!sent) g_subprocess_force_exit (authentication);
  busy = TRUE;
  status_text = "正在验证本机密码…";
  AuthAttempt *attempt = g_new0 (AuthAttempt, 1);
  attempt->generation = generation;
  g_subprocess_wait_async (authentication, NULL, auth_done, attempt);
  redraw ();
}

void meta_native_local_lock_key (guint key, gboolean down, gboolean fresh)
{
  if (!running || !key_state || key >= KEY_CNT || !fresh) return;
  if (down) held[key]++;
  else if (held[key]) held[key]--;
  else return;
  if (held[key] > 1 || (!down && held[key])) return;
  xkb_state_update_key (key_state, key + 8, down ? XKB_KEY_DOWN : XKB_KEY_UP);
  if (!down || busy) return;
  xkb_keysym_t sym = xkb_state_key_get_one_sym (key_state, key + 8);
  if (sym == XKB_KEY_Return || sym == XKB_KEY_KP_Enter) { submit (); return; }
  if (sym == XKB_KEY_Escape) clear_password ();
  else if (sym == XKB_KEY_BackSpace)
    {
      char *last = g_utf8_find_prev_char (password, password + strlen (password));
      if (last) explicit_bzero (last, strlen (last));
    }
  else if (!xkb_state_mod_name_is_active (key_state, XKB_MOD_NAME_CTRL, XKB_STATE_MODS_EFFECTIVE) &&
           !xkb_state_mod_name_is_active (key_state, XKB_MOD_NAME_ALT, XKB_STATE_MODS_EFFECTIVE))
    {
      char text[64] = {0};
      int length = xkb_state_key_get_utf8 (key_state, key + 8, text, sizeof text);
      if (length > 0 && length < sizeof text &&
          g_unichar_isprint (g_utf8_get_char (text)) &&
          strlen (password) + length < sizeof password - 1)
        strcat (password, text);
      explicit_bzero (text, sizeof text);
    }
  redraw ();
}

static void label (cairo_t *cr, const char *text, int size, int y, double alpha)
{
  PangoLayout *layout = pango_cairo_create_layout (cr);
  PangoFontDescription *font = pango_font_description_from_string ("Sans");
  pango_font_description_set_absolute_size (font, size * PANGO_SCALE);
  pango_layout_set_font_description (layout, font);
  pango_layout_set_text (layout, text, -1);
  pango_layout_set_width (layout, 760 * PANGO_SCALE);
  pango_layout_set_alignment (layout, PANGO_ALIGN_CENTER);
  cairo_set_source_rgba (cr, 1, 1, 1, alpha);
  cairo_move_to (cr, 20, y);
  pango_cairo_show_layout (cr, layout);
  pango_font_description_free (font);
  g_object_unref (layout);
}

void meta_native_local_lock_paint (ClutterStageView *view)
{
  if (!running) return;
  if (!view) return; /* Offscreen/remote scene must never include this UI. */
  CoglFramebuffer *fb = clutter_stage_view_get_framebuffer (view);
  CoglContext *cogl = cogl_framebuffer_get_context (fb);
  if (dirty || !texture)
    {
      cairo_surface_t *surface = cairo_image_surface_create (CAIRO_FORMAT_ARGB32, 800, 600);
      cairo_t *cr = cairo_create (surface);
      cairo_set_source_rgb (cr, .075, .085, .11);
      cairo_paint (cr);
      GDateTime *now = g_date_time_new_now_local ();
      char *clock = g_date_time_format (now, "%H:%M");
      char *date = g_date_time_format (now, "%Y年%m月%d日");
      label (cr, clock, 76, 30, 1);
      label (cr, date, 20, 130, .75);
      label (cr, g_get_user_name (), 30, 205, 1);
      label (cr, recovery_mode ? "请验证本机密码以接回桌面" : "远程会话正在使用此电脑", 18, 255, .72);
      cairo_set_source_rgb (cr, .17, .18, .22);
      cairo_rectangle (cr, 200, 315, 400, 52);
      cairo_fill (cr);
      GString *dots = g_string_new (NULL);
      for (int i = 0; i < MIN (g_utf8_strlen (password, -1), 24); i++) g_string_append (dots, "●");
      label (cr, *dots->str ? dots->str : "密码", 19, 326, *dots->str ? 1 : .5);
      g_string_free (dots, TRUE);
      label (cr, status_text, 17, 395, .85);
      if (xkb_state_mod_name_is_active (key_state, XKB_MOD_NAME_CAPS, XKB_STATE_MODS_LOCKED))
        label (cr, "Caps Lock 已开启", 15, 435, .7);
      label (cr, recovery_mode ? "桌面和应用将继续保留" : "验证成功后，远程连接将断开", 15, 480, .5);
      cairo_surface_flush (surface);
      g_clear_object (&texture);
      texture = COGL_TEXTURE (cogl_texture_2d_new_from_data (cogl, 800, 600,
        COGL_PIXEL_FORMAT_BGRA_8888_PRE, cairo_image_surface_get_stride (surface),
        cairo_image_surface_get_data (surface), NULL));
      cairo_destroy (cr);
      cairo_surface_destroy (surface);
      g_date_time_unref (now);
      g_free (clock); g_free (date);
      dirty = FALSE;
    }
  if (!texture) return; /* Underlying physical surface remains black. */
  /* Paint in physical framebuffer pixels, outside the remote stage actor's
   * allocation, camera and clip. Restore all matrix/viewport state afterwards. */
  graphene_matrix_t modelview, projection, identity;
  float viewport[4];
  int width = cogl_framebuffer_get_width (fb);
  int height = cogl_framebuffer_get_height (fb);
  cogl_framebuffer_get_modelview_matrix (fb, &modelview);
  cogl_framebuffer_get_projection_matrix (fb, &projection);
  cogl_framebuffer_get_viewport4fv (fb, viewport);
  graphene_matrix_init_identity (&identity);
  cogl_framebuffer_set_modelview_matrix (fb, &identity);
  cogl_framebuffer_set_viewport (fb, 0, 0, width, height);
  cogl_framebuffer_orthographic (fb, 0, 0, width, height, -1, 1);
  float scale = MIN (1.0f, MIN (width / 800.0f, height / 600.0f));
  float x = (width - 800 * scale) / 2;
  float y = (height - 600 * scale) / 2;
  CoglPipeline *pipeline = cogl_pipeline_new (cogl);
  cogl_pipeline_set_layer_texture (pipeline, 0, texture);
  cogl_framebuffer_draw_textured_rectangle (fb, pipeline, x, y,
    x + 800 * scale, y + 600 * scale, 0, 0, 1, 1);
  g_object_unref (pipeline);
  cogl_framebuffer_set_modelview_matrix (fb, &modelview);
  cogl_framebuffer_set_projection_matrix (fb, &projection);
  cogl_framebuffer_set_viewport (fb, viewport[0], viewport[1], viewport[2], viewport[3]);
}
