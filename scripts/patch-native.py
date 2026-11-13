#!/usr/bin/env python3
"""Reproducible patch for Mutter 50.1, including the original physical seat."""
from pathlib import Path
import difflib
import hashlib
import json
import subprocess

root = Path(__file__).resolve().parents[1]
source = root / 'vendor/mutter'
revision = '8e9bb806da4a6a4fc87aa4a481b228dba6735cbd'
assert subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() == revision
changes = {}
originals = {}


def edit(name, old, new):
    if name not in changes:
        originals[name] = subprocess.check_output(['git', '-C', str(source), 'show', f'HEAD:{name}'], text=True)
        changes[name] = originals[name]
    assert changes[name].count(old) == 1, (name, old[:80])
    changes[name] = changes[name].replace(old, new)


# Preserve the distribution fixes added since the previously verified build.
# Apply to pristine upstream text in a temporary tree, never to live edits.
import tempfile
updates = root / 'patches/ubuntu-50.1-0ubuntu2.4'
with tempfile.TemporaryDirectory(prefix='native-mutter-base-') as directory:
    temporary = Path(directory)
    for item in json.loads((updates / 'series.json').read_text()):
        patch_file = updates / item['file']
        assert hashlib.sha256(patch_file.read_bytes()).hexdigest() == item['sha256']
        stats = subprocess.check_output(['git', 'apply', '--numstat', str(patch_file)], text=True)
        for line in stats.splitlines():
            name = line.split('\t', 2)[2]
            if name in originals:
                continue
            assert not Path(name).is_absolute() and '..' not in Path(name).parts
            base = subprocess.run(['git', '-C', str(source), 'show', f'HEAD:{name}'],
                                  capture_output=True, text=True)
            originals[name] = base.stdout if base.returncode == 0 else ''
            if base.returncode == 0:
                target = temporary / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(base.stdout)
        subprocess.run(['git', 'apply', '--whitespace=nowarn', str(patch_file)],
                       cwd=temporary, check=True)
    for name in originals:
        changes[name] = (temporary / name).read_text()

for ext in ('h', 'c'):
    name = f'src/backends/meta-native-privacy.{ext}'
    originals[name] = ''
    changes[name] = (root / f'takeover/native-privacy.{ext}').read_text()

for ext in ('h', 'c'):
    name = f'src/backends/meta-native-local-lock.{ext}'
    originals[name] = ''
    changes[name] = (root / f'takeover/native-local-lock.{ext}').read_text()

edit('src/meson.build', "  'backends/meta-stage.c',", "  'backends/meta-native-privacy.c',\n  'backends/meta-native-privacy.h',\n  'backends/meta-native-local-lock.c',\n  'backends/meta-native-local-lock.h',\n  'backends/meta-stage.c',")
edit('src/meson.build', '  cairo_dep,', '  cairo_dep,\n  pangocairo_dep,')
edit('src/core/meta-context.c', '#include "config.h"', '#include "config.h"\n#include "backends/meta-native-privacy.h"')
edit('src/core/meta-context.c', '  g_signal_emit (context, signals[STARTED], 0);',
     '  g_signal_emit (context, signals[STARTED], 0);\n  meta_native_privacy_init (context);')
edit('clutter/clutter/clutter-stage-view-private.h',
     'void clutter_stage_view_invalidate_input_devices (ClutterStageView *view);',
     'CLUTTER_EXPORT\nvoid clutter_stage_view_invalidate_input_devices (ClutterStageView *view);')
edit('src/backends/meta-stage.c', '#include "config.h"', '#include "config.h"\n#include "backends/meta-native-privacy.h"')
edit('src/backends/meta-stage.c', '#include "clutter/clutter-mutter.h"',
     '#include "clutter/clutter-mutter.h"\n#include "clutter/clutter-stage-view-private.h"')
edit('src/backends/meta-stage.c', '''  notify_watchers_for_mode (meta_stage, view, NULL, frame,
                            META_STAGE_WATCH_BEFORE_PAINT);''', '''  /* The physical authentication surface is independent of remote scene
   * damage. Clear/draw the whole physical output, not the app's damage clip. */
  if (meta_native_privacy_active () && meta_native_privacy_view_is_physical (view))
    clutter_stage_view_add_redraw_clip (view, NULL);

  notify_watchers_for_mode (meta_stage, view, NULL, frame,
                            META_STAGE_WATCH_BEFORE_PAINT);''')
edit('src/backends/meta-stage.c', '  CLUTTER_ACTOR_CLASS (meta_stage_parent_class)->paint (actor, paint_context);', '''  gboolean physical_private;

  /* Offscreen PipeWire repaint has no stage view and sees the original scene.
   * Never attach an actor overlay: that would also blacken remote capture.
   * Extra remote-only monitors have views but are never shown locally. */
  physical_private = meta_native_privacy_active () &&
                     clutter_paint_context_get_stage_view (paint_context) != NULL &&
                     meta_native_privacy_view_is_physical (
                       clutter_paint_context_get_stage_view (paint_context));
  if (physical_private)
    {
      cogl_framebuffer_clear4f (clutter_paint_context_get_framebuffer (paint_context),
                                COGL_BUFFER_BIT_COLOR, 0, 0, 0, 1);
      meta_native_privacy_painted (clutter_paint_context_get_stage_view (paint_context),
                                   clutter_paint_context_get_frame (paint_context));
    }
  else
    CLUTTER_ACTOR_CLASS (meta_stage_parent_class)->paint (actor, paint_context);''')
edit('src/backends/meta-stage.c', '  g_list_foreach (stage->overlays, (GFunc) meta_overlay_paint, paint_context);',
     '  if (!physical_private)\n    g_list_foreach (stage->overlays, (GFunc) meta_overlay_paint, paint_context);')
edit('src/backends/meta-stage.c',
     '  notify_watchers_for_mode (meta_stage, view, redraw_clip, frame,\n                            META_STAGE_WATCH_AFTER_PAINT);',
     '''  /* The stage actor clips to the remote desktop allocation. Draw the
   * physical login surface after that clip has been popped, so a smaller
   * remote desktop cannot crop a larger local monitor's password UI. */
  if (meta_native_privacy_active () && meta_native_privacy_view_is_physical (view))
    {
      cogl_framebuffer_clear4f (clutter_stage_view_get_framebuffer (view),
                                COGL_BUFFER_BIT_COLOR, 0, 0, 0, 1);
      meta_native_privacy_paint_local (view);
      /* The offscreen desktop can add popups without painting the physical
       * actor tree. Invalidate hit testing for the newly painted scene. */
      clutter_stage_view_invalidate_input_devices (view);
    }

  notify_watchers_for_mode (meta_stage, view, redraw_clip, frame,
                            META_STAGE_WATCH_AFTER_PAINT);''')
edit('src/backends/native/meta-seat-impl.c', '#include "config.h"', '#include "config.h"\n#include "backends/meta-native-privacy.h"')
edit('src/backends/native/meta-seat-impl.c', '''  if (process_base_event (seat_impl, event))''', '''  /* Observe device removal as well, to release tracked physical holds. */
  if (meta_native_privacy_filter_input (event))
    return;

  if (process_base_event (seat_impl, event))''')
# libinput seat counts include quarantined physical keys. Use only presses
# delivered to this session, so a stuck Ctrl on one keyboard cannot suppress
# a fresh Ctrl on another keyboard after returning to the local login screen.
edit('src/backends/native/meta-seat-impl.c',
     'libinput_event_keyboard_get_seat_key_count (key_event);',
     'meta_native_privacy_key_count (key, libinput_event_keyboard_get_seat_key_count (key_event));')
edit('src/backends/native/meta-seat-impl.c',
     'libinput_event_pointer_get_seat_button_count (button_event);',
     'meta_native_privacy_key_count (button, libinput_event_pointer_get_seat_button_count (button_event));')
edit('src/core/events.c', '#include "backends/native/meta-backend-native.h"',
     '#include "backends/native/meta-backend-native.h"\n#include "backends/native/meta-input-thread.h"\n#include "backends/meta-native-privacy.h"')
edit('src/core/events.c', '  if (meta_display_process_captured_input (display, event))', '''#ifdef HAVE_NATIVE_BACKEND
  /* Also discard physical events queued just before the privacy transition.
   * Raw libinput suppression prevents modifier/repeat state changes. */
  source_device = clutter_event_get_source_device (event);
  if (meta_native_privacy_active () && !meta_native_privacy_is_cleanup_event (event) && source_device &&
      META_IS_INPUT_DEVICE_NATIVE (source_device) &&
      meta_input_device_native_get_libinput_device (source_device))
    return CLUTTER_EVENT_STOP;
#endif

  if (meta_display_process_captured_input (display, event))''')

# Remote logical coordinates can extend beyond every physical stage view.
# Picking uses the same actor tree/ray as offscreen painting; no DRM view is
# needed (the current pick context does not consume its view argument).
edit('clutter/clutter/clutter-stage-private.h',
     'void clutter_stage_invalidate_devices (ClutterStage *stage);',
     'CLUTTER_EXPORT\nvoid clutter_stage_set_offscreen_picking (ClutterStage *stage, gboolean enabled);\n\nvoid clutter_stage_invalidate_devices (ClutterStage *stage);')
edit('clutter/clutter/clutter-stage.c',
     '  gboolean update_scheduled;',
     '  gboolean update_scheduled;\n  gboolean offscreen_picking;')
edit('clutter/clutter/clutter-stage.c',
     'void\nclutter_stage_invalidate_devices (ClutterStage *stage)',
     """void
clutter_stage_set_offscreen_picking (ClutterStage *stage, gboolean enabled)
{
  ClutterStagePrivate *priv = clutter_stage_get_instance_private (stage);
  priv->offscreen_picking = enabled;
  clutter_stage_invalidate_devices (stage);
}

void
clutter_stage_invalidate_devices (ClutterStage *stage)""")
edit('clutter/clutter/clutter-stage.c',
     '  if (view)\n    return _clutter_stage_do_pick_on_view',
     '  if (view || priv->offscreen_picking)\n    return _clutter_stage_do_pick_on_view')
edit('clutter/clutter/clutter-stage.c',
     '  if (pointer_view && pointer_view == view)',
     """  ClutterStagePrivate *priv = clutter_stage_get_instance_private (stage);
  if ((pointer_view && pointer_view == view) ||
      (!pointer_view && priv->offscreen_picking &&
       view == clutter_stage_peek_stage_views (stage)->data))""")

# A temporary remote viewport belongs to the existing logical desktop. Physical
# CRTCs keep real hardware modes and paint only the local authentication UI.
# Offscreen monitor capture uses the logical rectangle, independently of CRTCs.
edit('src/backends/meta-monitor-config-manager.h',
     '  gboolean is_presentation;\n} MetaLogicalMonitorConfig;',
     '  gboolean is_presentation;\n  gboolean native_takeover_viewport;\n  guint native_viewport_width, native_viewport_height;\n} MetaLogicalMonitorConfig;')
for file in ('src/backends/meta-monitor-manager.c', 'src/backends/meta-monitor-config-manager.c'):
    edit(file, '#include "config.h"', '#include "config.h"\n#include "backends/meta-native-privacy.h"')
edit('src/backends/meta-monitor-config-manager.c',
     '  first_monitor_config = logical_monitor_config->monitor_configs->data;\n  mode_width =',
     '''  if (logical_monitor_config->native_takeover_viewport)
    {
      /* Remote-only monitors left of/above the viewport may offset it. */
      if (!meta_native_privacy_has_lease () ||
          logical_monitor_config->native_viewport_width < 200 ||
          logical_monitor_config->native_viewport_width > 8192 ||
          logical_monitor_config->native_viewport_height < 200 ||
          logical_monitor_config->native_viewport_height > 8192 ||
          !isfinite (scale) || scale < 1.0f || scale > 2.0f ||
          floorf (scale * 4) != scale * 4 ||
          logical_monitor_config->transform != 0 ||
          !logical_monitor_config->is_primary)
        {
          g_set_error (error, G_IO_ERROR, G_IO_ERROR_FAILED,
                       "Invalid or unprotected native remote viewport");
          return FALSE;
        }
      /* Every physical mode/connector is still checked for applicability. */
      return TRUE;
    }

  first_monitor_config = logical_monitor_config->monitor_configs->data;
  mode_width =''')
edit('src/backends/meta-monitor-config-store.c',
     '  g_hash_table_replace (config_store->configs,\n                        config->key, g_object_ref (config));',
     '''  /* A remote viewport is never a persistent physical monitor setup. */
  for (GList *l = config->logical_monitor_configs; l; l = l->next)
    if (((MetaLogicalMonitorConfig *) l->data)->native_takeover_viewport)
      return;

  g_hash_table_replace (config_store->configs,
                        config->key, g_object_ref (config));''')
file = 'src/backends/meta-monitor-manager.c'
edit(file, '  scale = (float) scale_d;',
     '''  scale = (float) scale_d;
  if (native_size && (!isfinite (scale) || scale < 1.0f || scale > 2.0f))
    {
      g_variant_iter_free (monitor_configs_iter);
      g_set_error (error, G_IO_ERROR, G_IO_ERROR_FAILED, "Invalid remote scale");
      goto err;
    }''')
edit(file, '  if (!find_monitor_mode_scale (manager,',
     '  if (!native_size && !find_monitor_mode_scale (manager,')
edit(file, '          if (!meta_monitor_manager_is_scale_supported_for_config (manager,',
     '          if (!logical_monitor_config->native_takeover_viewport &&\n              !meta_monitor_manager_is_scale_supported_for_config (manager,')
edit(file, '                                            MetaLogicalMonitorLayoutMode layout_mode,\n                                            GError                     **error)',
     '                                            MetaLogicalMonitorLayoutMode layout_mode,\n                                            GVariant                    *native_size,\n                                            GError                     **error)')
edit(file, '  if (!meta_verify_logical_monitor_config (logical_monitor_config,',
     '''  if (native_size)
    {
      guint32 remote_width, remote_height;
      g_variant_get (native_size, "(uu)", &remote_width, &remote_height);
      if (remote_width < 200 || remote_width > 8192 ||
          remote_height < 200 || remote_height > 8192)
        {
          meta_logical_monitor_config_free (logical_monitor_config);
          g_set_error (error, G_IO_ERROR, G_IO_ERROR_FAILED, "Invalid remote scale");
          return NULL;
        }
      logical_monitor_config->native_viewport_width = remote_width;
      logical_monitor_config->native_viewport_height = remote_height;
      logical_monitor_config->layout.width = (int) ceilf (remote_width / scale);
      logical_monitor_config->layout.height = (int) ceilf (remote_height / scale);
      logical_monitor_config->native_takeover_viewport = TRUE;
    }

  if (!meta_verify_logical_monitor_config (logical_monitor_config,''')
edit(file, '  g_autoptr (GVariant) layout_mode_variant = NULL;',
     '  g_autoptr (GVariant) layout_mode_variant = NULL;\n  g_autoptr (GVariant) native_size = NULL;')
edit(file, '  g_variant_iter_init (&logical_monitor_configs_iter,\n                       logical_monitor_configs_variant);',
     '''  native_size = g_variant_lookup_value (properties_variant,
                                        "native-takeover-size", NULL);
  if (native_size &&
      (!g_variant_is_of_type (native_size, G_VARIANT_TYPE ("(uu)")) ||
       !meta_native_privacy_has_lease () ||
       (method != META_MONITORS_CONFIG_METHOD_VERIFY &&
        method != META_MONITORS_CONFIG_METHOD_TEMPORARY) ||
       g_variant_n_children (logical_monitor_configs_variant) != 1))
    {
      g_dbus_method_invocation_return_error (invocation, G_DBUS_ERROR,
                                             G_DBUS_ERROR_INVALID_ARGS,
                                             "Native viewport requires one protected temporary desktop");
      return G_DBUS_METHOD_INVOCATION_HANDLED;
    }

  g_variant_iter_init (&logical_monitor_configs_iter,
                       logical_monitor_configs_variant);''')
# GNOME Settings (or any other client) knows nothing about the viewport. Its
# configuration used to replace the viewport with the physical modes while the
# capture stream still had the client's size, which crashed the compositor.
# During a lease such a request can only change the scale of the remote
# desktop; the lease owner marks its own release of the viewport.
edit(file, '''  native_size = g_variant_lookup_value (properties_variant,
                                        "native-takeover-size", NULL);''',
     '''  native_size = g_variant_lookup_value (properties_variant,
                                        "native-takeover-size", NULL);
  MetaLogicalMonitorConfig *native_foreign_viewport = NULL;
  native_foreign_scale = FALSE;
  if (!native_size && g_getenv ("MUTTER_NATIVE_TAKEOVER") &&
      meta_native_privacy_has_lease ())
    {
      g_autoptr (GVariant) native_release =
        g_variant_lookup_value (properties_variant,
                                "native-takeover-release", NULL);
      MetaMonitorsConfig *native_current =
        meta_monitor_config_manager_get_current (manager->config_manager);
      MetaLogicalMonitorConfig *native_viewport = NULL;

      for (GList *native_l = native_current && !native_release ?
                               native_current->logical_monitor_configs : NULL;
           native_l; native_l = native_l->next)
        {
          MetaLogicalMonitorConfig *logical = native_l->data;
          if (logical->native_takeover_viewport)
            native_viewport = logical;
        }
      if (native_viewport)
        {
          if (g_variant_n_children (logical_monitor_configs_variant) != 1)
            {
              g_dbus_method_invocation_return_error (invocation, G_DBUS_ERROR,
                                                     G_DBUS_ERROR_INVALID_ARGS,
                                                     "The remote session owns the display layout; only its scale can be changed");
              return G_DBUS_METHOD_INVOCATION_HANDLED;
            }
          native_size = g_variant_ref_sink (
            g_variant_new ("(uu)", native_viewport->native_viewport_width,
                           native_viewport->native_viewport_height));
          native_foreign_scale = TRUE;
          native_foreign_viewport = native_viewport;
          /* Never a persistent physical setup, and no confirmation dialog. */
          if (method == META_MONITORS_CONFIG_METHOD_PERSISTENT)
            method = META_MONITORS_CONFIG_METHOD_TEMPORARY;
        }
    }''')
# Display panels offer the scales of the physical mode (for example 1.4953);
# the viewport takes the nearest quarter step.
edit(file, '''      logical_monitor_configs = g_list_append (logical_monitor_configs,
                                               logical_monitor_config);''',
     '''      if (native_foreign_viewport &&
          !native_request_keeps_monitors (logical_monitor_config,
                                          native_foreign_viewport))
        {
          meta_logical_monitor_config_free (logical_monitor_config);
          g_list_free_full (logical_monitor_configs,
                            (GDestroyNotify) meta_logical_monitor_config_free);
          g_dbus_method_invocation_return_error (invocation, G_DBUS_ERROR,
                                                 G_DBUS_ERROR_INVALID_ARGS,
                                                 "The remote session owns the display layout; only its scale can be changed");
          return G_DBUS_METHOD_INVOCATION_HANDLED;
        }

      logical_monitor_configs = g_list_append (logical_monitor_configs,
                                               logical_monitor_config);''')
edit(file, '''  scale = (float) scale_d;
  if (native_size && (''', '''  scale = (float) scale_d;
  if (native_size && native_foreign_scale)
    scale = CLAMP (roundf (scale * 4) / 4, 1.0f, 2.0f);
  if (native_size && (''')
edit(file, '                                                    layout_mode,\n                                                    &error);',
     '                                                    layout_mode,\n                                                    native_size,\n                                                    &error);')
edit(file, '  capabilities = meta_monitor_manager_get_capabilities (manager);\n\n  g_variant_builder_add (&properties_builder,',
     '''  capabilities = meta_monitor_manager_get_capabilities (manager);

  if (g_getenv ("MUTTER_NATIVE_TAKEOVER"))
    {
      MetaMonitorsConfig *current =
        meta_monitor_config_manager_get_current (manager->config_manager);
      g_variant_builder_add (&properties_builder, "{sv}",
                             "native-takeover-viewport-supported",
                             g_variant_new_boolean (TRUE));
      g_variant_builder_add (&properties_builder, "{sv}",
                             "native-takeover-scale-supported",
                             g_variant_new_boolean (TRUE));
      g_variant_builder_add (&properties_builder, "{sv}",
                             "native-takeover-multimonitor-supported",
                             g_variant_new_boolean (TRUE));
      for (GList *native_l = current ? current->logical_monitor_configs : NULL;
           native_l; native_l = native_l->next)
        {
          MetaLogicalMonitorConfig *logical = native_l->data;
          GVariantBuilder extra;
          if (!logical->native_takeover_viewport)
            continue;
          g_variant_builder_add (&properties_builder, "{sv}",
                                 "native-takeover-size",
                                 g_variant_new ("(uu)", logical->native_viewport_width,
                                                logical->native_viewport_height));
          g_variant_builder_init (&extra, G_VARIANT_TYPE ("a(iiuu)"));
          for (unsigned int native_i = 0; native_i < n_native_extra; native_i++)
            g_variant_builder_add (&extra, "(iiuu)",
                                   native_extra[native_i].x, native_extra[native_i].y,
                                   native_extra[native_i].width, native_extra[native_i].height);
          g_variant_builder_add (&properties_builder, "{sv}",
                                 "native-takeover-extra", g_variant_builder_end (&extra));
          break;
        }
    }

  g_variant_builder_add (&properties_builder,''')

# Fractional logical sizes round upward. Capture into the exact client pixel
# dimensions rather than writing a rounded-up image into a smaller stride.
file = 'src/backends/meta-screen-cast-monitor-stream.c'
edit(file, '#include "config.h"', '#include "config.h"\n#include "backends/meta-native-privacy.h"')
edit(file, '  MetaLogicalMonitor *logical_monitor;\n};',
     '  MetaLogicalMonitor *logical_monitor;\n  int native_pixel_width, native_pixel_height;\n};')
edit(file, '      g_set_object (&monitor_stream->logical_monitor, logical_monitor);',
     '''      g_set_object (&monitor_stream->logical_monitor, logical_monitor);
      meta_native_privacy_get_viewport_size (logical_monitor,
        &monitor_stream->native_pixel_width, &monitor_stream->native_pixel_height);''')
edit(file, '''  if (!mtk_rectangle_equal (&new_logical_monitor->rect,
                            &monitor_stream->logical_monitor->rect))
    return FALSE;''',
     '''  int width = 0, height = 0;
  gboolean native_viewport = meta_native_privacy_get_viewport_size (
    new_logical_monitor, &width, &height);
  if (native_viewport)
    {
      /* Scale changes keep the capture stream/mapping ID. Rebuild only
       * the EI viewport so input uses the new logical coordinates. */
      if (width != monitor_stream->native_pixel_width ||
          height != monitor_stream->native_pixel_height)
        return FALSE;
    }
  else if (!mtk_rectangle_equal (&new_logical_monitor->rect,
                                 &monitor_stream->logical_monitor->rect))
    return FALSE;''')
edit(file, '  g_set_object (&monitor_stream->logical_monitor, new_logical_monitor);',
     '''  g_set_object (&monitor_stream->logical_monitor, new_logical_monitor);
  if (native_viewport)
    meta_eis_viewport_notify_changed (META_EIS_VIEWPORT (monitor_stream));''')
file = 'src/backends/meta-screen-cast-monitor-stream-src.c'
edit(file, '#include "config.h"', '#include "config.h"\n#include "backends/meta-native-privacy.h"')
edit(file, '  *height = (int) roundf (logical_monitor->rect.height * scale);',
     '''  *height = (int) roundf (logical_monitor->rect.height * scale);
  meta_native_privacy_get_viewport_size (logical_monitor, width, height);''')
edit(file, '  if (!clutter_stage_paint_to_buffer (stage, &logical_monitor->rect, scale,',
     '''  int viewport_width, viewport_height;
  if (meta_native_privacy_get_viewport_size (logical_monitor,
                                            &viewport_width, &viewport_height))
    {
      if (width != viewport_width || height != viewport_height || stride < width * 4)
        {
          g_set_error (error, G_IO_ERROR, G_IO_ERROR_FAILED, "Stale remote capture size");
          return FALSE;
        }
      CoglTexture *texture = COGL_TEXTURE (cogl_texture_2d_new_with_size (
        clutter_backend_get_cogl_context (meta_backend_get_clutter_backend (backend)),
        width, height));
      CoglFramebuffer *fb = COGL_FRAMEBUFFER (cogl_offscreen_new_with_texture (texture));
      g_object_unref (texture);
      if (!cogl_framebuffer_allocate (fb, error))
        { g_object_unref (fb); return FALSE; }
      clutter_stage_paint_to_framebuffer (stage, fb, &logical_monitor->rect, scale,
                                          color_state, paint_flags);
      CoglBitmap *bitmap = cogl_bitmap_new_for_data (cogl_framebuffer_get_context (fb),
        width, height, COGL_PIXEL_FORMAT_CAIRO_ARGB32_COMPAT, stride, data);
      gboolean success = cogl_framebuffer_read_pixels_into_bitmap (
        fb, 0, 0, COGL_READ_PIXELS_COLOR_BUFFER, bitmap);
      g_object_unref (bitmap);
      g_object_unref (fb);
      return success;
    }

  /* clutter_stage_paint_to_buffer() reads rect * scale pixels back, whatever
   * the size of the stream buffer is. */
  if (width != (int) roundf (logical_monitor->rect.width * scale) ||
      height != (int) roundf (logical_monitor->rect.height * scale))
    {
      g_set_error (error, G_IO_ERROR, G_IO_ERROR_FAILED, "Stale capture size");
      return FALSE;
    }

  if (!clutter_stage_paint_to_buffer (stage, &logical_monitor->rect, scale,''')

# Extra client monitors are remote-only Mutter virtual monitors beside the
# viewport. GRD creates them with RecordVirtual; Mutter keeps the viewport and
# places them at the client's positions. They are never physical outputs.
file = 'src/backends/meta-monitor-config-manager.h'
edit(file, 'META_EXPORT_TEST\ngboolean meta_verify_monitors_config (MetaMonitorsConfig *config,',
     '''/* Client-pixel geometry of a remote-only monitor, relative to the viewport. */
typedef struct _MetaNativeExtraMonitor
{
  int x, y, width, height;
} MetaNativeExtraMonitor;

#define META_NATIVE_MAX_EXTRA_MONITORS 7

/* Remote-only monitors are virtual monitors whose screen-cast stream carries
 * the native takeover slot tag from the leased GRD. Other screen casts (for
 * example a local Miracast sink) and headless test outputs are not. */
void meta_monitor_set_native_remote_slot (const char *connector,
                                          int         slot);
int meta_monitor_get_native_remote_slot (MetaMonitor *monitor);
gboolean meta_monitor_is_native_remote_only (MetaMonitor *monitor);

MetaMonitorsConfig * meta_monitor_config_manager_native_extend (MetaMonitorManager           *monitor_manager,
                                                                MetaMonitorsConfig           *config,
                                                                const MetaNativeExtraMonitor *hints,
                                                                unsigned int                  n_hints);

META_EXPORT_TEST
gboolean meta_verify_monitors_config (MetaMonitorsConfig *config,''')
file = 'src/backends/meta-monitor-config-manager.c'
edit(file, '''                       "For lease monitor must be explicitly disabled");
          return FALSE;
        }
    }

  return TRUE;
}''', '''                       "For lease monitor must be explicitly disabled");
          return FALSE;
        }
    }

  return TRUE;
}

static GHashTable *native_remote_slots;

void
meta_monitor_set_native_remote_slot (const char *connector,
                                     int         slot)
{
  if (!native_remote_slots)
    native_remote_slots = g_hash_table_new_full (g_str_hash, g_str_equal,
                                                 g_free, NULL);
  if (slot < 0)
    g_hash_table_remove (native_remote_slots, connector);
  else
    g_hash_table_insert (native_remote_slots, g_strdup (connector),
                         GINT_TO_POINTER (slot + 1));
}

int
meta_monitor_get_native_remote_slot (MetaMonitor *monitor)
{
  if (!native_remote_slots || !meta_monitor_is_virtual (monitor))
    return -1;

  return GPOINTER_TO_INT (g_hash_table_lookup (native_remote_slots,
                                               meta_monitor_get_spec (monitor)->connector)) - 1;
}

gboolean
meta_monitor_is_native_remote_only (MetaMonitor *monitor)
{
  return meta_monitor_get_native_remote_slot (monitor) >= 0;
}

static int
native_logical_coordinate (int   value,
                           float scale)
{
  return value >= 0 ? (int) ceilf (value / scale) : -(int) ceilf (-value / scale);
}

static guint64
native_virtual_sequence (MetaMonitor *monitor)
{
  const char *connector = meta_monitor_get_spec (monitor)->connector;

  return g_str_has_prefix (connector, "Meta-") ?
    g_ascii_strtoull (connector + 5, NULL, 10) : G_MAXUINT64;
}

static int
compare_native_virtual (gconstpointer a,
                        gconstpointer b)
{
  guint64 first = native_virtual_sequence (*(MetaMonitor **) a);
  guint64 second = native_virtual_sequence (*(MetaMonitor **) b);

  return first < second ? -1 : first > second;
}

static gboolean
native_monitor_is_passenger (MetaLogicalMonitorConfig *viewport,
                             MetaMonitor              *monitor)
{
  for (GList *l = viewport->monitor_configs; l; l = l->next)
    {
      MetaMonitorConfig *monitor_config = l->data;

      if (meta_monitor_spec_equals (monitor_config->monitor_spec,
                                    meta_monitor_get_spec (monitor)))
        return TRUE;
    }

  return FALSE;
}

static MetaMonitorsConfig *
native_extended_config (MetaMonitorManager           *monitor_manager,
                        MetaMonitorsConfig           *config,
                        MetaLogicalMonitorConfig     *viewport,
                        GPtrArray                    *virtuals,
                        const MetaNativeExtraMonitor *hints,
                        unsigned int                  n_hints,
                        gboolean                      use_hints)
{
  gboolean used[META_NATIVE_MAX_EXTRA_MONITORS] = { FALSE };
  g_autoptr (GError) error = NULL;
  MetaLogicalMonitorConfig *primary;
  GList *logical_monitor_configs;
  GList *single;
  MetaMonitorsConfig *result;
  int row_x, min_x = 0, min_y = 0;

  single = g_list_append (NULL, viewport);
  logical_monitor_configs = meta_clone_logical_monitor_config_list (single);
  g_list_free (single);
  primary = logical_monitor_configs->data;
  primary->layout.x = 0;
  primary->layout.y = 0;
  row_x = primary->layout.width;

  /* A physical output unplugged during the lease leaves the viewport. */
  for (GList *l = primary->monitor_configs; l;)
    {
      GList *next = l->next;
      MetaMonitorConfig *monitor_config = l->data;

      if (!meta_monitor_manager_get_monitor_from_spec (monitor_manager,
                                                       monitor_config->monitor_spec))
        {
          meta_monitor_config_free (monitor_config);
          primary->monitor_configs = g_list_delete_link (primary->monitor_configs, l);
        }
      l = next;
    }
  if (!primary->monitor_configs)
    goto failed;

  for (unsigned int i = 0; i < virtuals->len; i++)
    {
      MetaMonitor *monitor = g_ptr_array_index (virtuals, i);
      MetaMonitorMode *mode = meta_monitor_get_preferred_mode (monitor);
      MetaLogicalMonitorConfig *logical;
      float scale = viewport->scale;
      int width, height, logical_width, logical_height, x, y;
      int hint = -1;

      if (!mode)
        goto failed;

      meta_monitor_mode_get_resolution (mode, &width, &height);
      if (!meta_monitor_manager_is_scale_supported (monitor_manager,
                                                    config->layout_mode,
                                                    monitor, mode, scale))
        scale = 1.0f;
      scale_logical_monitor_width (config->layout_mode, scale, width, height,
                                   &logical_width, &logical_height);

      if (use_hints)
        {
          int slot = meta_monitor_get_native_remote_slot (monitor);

          if (slot >= 0 && (unsigned int) slot < n_hints && !used[slot] &&
              hints[slot].width == width && hints[slot].height == height)
            hint = slot;
          /* Without a usable slot, equal sizes keep creation order. */
          for (unsigned int j = 0; j < n_hints && hint < 0; j++)
            {
              if (!used[j] && hints[j].width == width && hints[j].height == height)
                hint = (int) j;
            }
          if (hint < 0)
            goto failed;

          used[hint] = TRUE;
          x = native_logical_coordinate (hints[hint].x, viewport->scale);
          y = native_logical_coordinate (hints[hint].y, viewport->scale);
        }
      else
        {
          x = row_x;
          y = 0;
          row_x += logical_width;
        }

      logical = g_new0 (MetaLogicalMonitorConfig, 1);
      *logical = (MetaLogicalMonitorConfig) {
        .layout = (MtkRectangle) {
          .x = x,
          .y = y,
          .width = logical_width,
          .height = logical_height,
        },
        .transform = MTK_MONITOR_TRANSFORM_NORMAL,
        .scale = scale,
        .monitor_configs = g_list_append (NULL,
                                          create_monitor_config (monitor, mode,
                                                                 META_COLOR_MODE_DEFAULT)),
      };
      logical_monitor_configs = g_list_append (logical_monitor_configs, logical);
      min_x = MIN (min_x, x);
      min_y = MIN (min_y, y);
    }

  for (GList *l = logical_monitor_configs; l; l = l->next)
    {
      MetaLogicalMonitorConfig *logical = l->data;

      logical->layout.x -= min_x;
      logical->layout.y -= min_y;
    }

  result = meta_monitors_config_new (monitor_manager, logical_monitor_configs,
                                     config->layout_mode,
                                     META_MONITORS_CONFIG_FLAG_NONE);
  if (meta_verify_monitors_config (result, monitor_manager, &error))
    return result;

  meta_topic (META_DEBUG_BACKEND, "Native remote monitor layout rejected: %s",
              error->message);
  g_object_unref (result);
  return NULL;

failed:
  g_list_free_full (logical_monitor_configs,
                    (GDestroyNotify) meta_logical_monitor_config_free);
  return NULL;
}

/* Keep the authenticated viewport and add every remote-only virtual monitor.
 * Physical outputs remain viewport passengers. Client positions are used
 * when every virtual monitor matches a hint; otherwise they form a row. */
MetaMonitorsConfig *
meta_monitor_config_manager_native_extend (MetaMonitorManager           *monitor_manager,
                                          MetaMonitorsConfig           *config,
                                          const MetaNativeExtraMonitor *hints,
                                          unsigned int                  n_hints)
{
  g_autoptr (GPtrArray) virtuals = g_ptr_array_new ();
  MetaLogicalMonitorConfig *viewport = NULL;
  MetaMonitorsConfig *result;

  for (GList *l = config ? config->logical_monitor_configs : NULL; l; l = l->next)
    {
      MetaLogicalMonitorConfig *logical = l->data;

      if (logical->native_takeover_viewport)
        viewport = logical;
    }
  if (!viewport)
    return NULL;

  for (GList *l = meta_monitor_manager_get_monitors (monitor_manager); l; l = l->next)
    {
      MetaMonitor *monitor = l->data;

      if (meta_monitor_is_native_remote_only (monitor) &&
          !native_monitor_is_passenger (viewport, monitor) &&
          virtuals->len < META_NATIVE_MAX_EXTRA_MONITORS)
        g_ptr_array_add (virtuals, monitor);
    }
  g_ptr_array_sort (virtuals, compare_native_virtual);

  n_hints = MIN (n_hints, META_NATIVE_MAX_EXTRA_MONITORS);
  result = native_extended_config (monitor_manager, config, viewport, virtuals,
                                   hints, n_hints, TRUE);
  if (!result)
    result = native_extended_config (monitor_manager, config, viewport, virtuals,
                                     hints, n_hints, FALSE);
  if (!result)
    {
      /* Never lose the protected viewport because of an extra monitor. */
      g_ptr_array_set_size (virtuals, 0);
      result = native_extended_config (monitor_manager, config, viewport, virtuals,
                                       hints, n_hints, FALSE);
    }

  return result;
}''')
file = 'src/backends/meta-monitor-manager.c'
edit(file, 'MetaMonitorsConfig *\nmeta_monitor_manager_ensure_configured (MetaMonitorManager *manager)\n{',
     '''/* Client placement of remote-only monitors for the current viewport lease. */
static MetaNativeExtraMonitor native_extra[META_NATIVE_MAX_EXTRA_MONITORS];
static unsigned int n_native_extra;

/* Set while a request that does not know the viewport (GNOME Settings) is
 * applied as a scale change of the remote desktop. */
static gboolean native_foreign_scale;

/* Such a request may change nothing but the scale: the same physical
 * monitors in the same modes as the passengers of the current viewport. */
static gboolean
native_request_keeps_monitors (MetaLogicalMonitorConfig *request,
                               MetaLogicalMonitorConfig *viewport)
{
  if (g_list_length (request->monitor_configs) !=
      g_list_length (viewport->monitor_configs))
    return FALSE;

  for (GList *l = request->monitor_configs; l; l = l->next)
    {
      MetaMonitorConfig *wanted = l->data;
      gboolean found = FALSE;

      for (GList *c = viewport->monitor_configs; c && !found; c = c->next)
        {
          MetaMonitorConfig *current = c->data;

          found = meta_monitor_spec_equals (wanted->monitor_spec,
                                            current->monitor_spec) &&
                  wanted->mode_spec->width == current->mode_spec->width &&
                  wanted->mode_spec->height == current->mode_spec->height &&
                  wanted->mode_spec->refresh_rate == current->mode_spec->refresh_rate &&
                  wanted->mode_spec->refresh_rate_mode == current->mode_spec->refresh_rate_mode &&
                  wanted->mode_spec->flags == current->mode_spec->flags;
        }
      if (!found)
        return FALSE;
    }

  return TRUE;
}

/* While an authenticated viewport is current, monitor changes must not
 * replace it with a stored or linear physical layout: remote-only monitors
 * are added or removed, unplugged passengers are dropped, and a newly
 * plugged physical output stays disabled until the lease ends. */
static MetaMonitorsConfig *
ensure_native_takeover_configured (MetaMonitorManager *manager)
{
  g_autoptr (GError) error = NULL;
  MetaMonitorsConfig *config;

  if (!g_getenv ("MUTTER_NATIVE_TAKEOVER") || !meta_native_privacy_has_lease ())
    return NULL;

  config = meta_monitor_config_manager_native_extend (
    manager, meta_monitor_config_manager_get_current (manager->config_manager),
    native_extra, n_native_extra);
  if (!config)
    return NULL;

  if (!meta_monitor_manager_apply_monitors_config (manager, config,
                                                   META_MONITORS_CONFIG_METHOD_TEMPORARY,
                                                   &error))
    {
      g_warning ("[NativeTakeover] Could not keep the remote viewport: %s",
                 error->message);
      g_object_unref (config);
      return NULL;
    }

  return config;
}

MetaMonitorsConfig *
meta_monitor_manager_ensure_configured (MetaMonitorManager *manager)
{''')
edit(file, '  use_stored_config = should_use_stored_config (manager);',
     '''  config = ensure_native_takeover_configured (manager);
  if (config)
    goto done;

  use_stored_config = should_use_stored_config (manager);''')
edit(file, '      is_for_lease = meta_monitor_is_for_lease (monitor);',
     '''      if (g_getenv ("MUTTER_NATIVE_TAKEOVER"))
        g_variant_builder_add (&monitor_properties_builder, "{sv}",
                               "native-takeover-remote-only",
                               g_variant_new_boolean (meta_monitor_is_native_remote_only (monitor)));

      is_for_lease = meta_monitor_is_for_lease (monitor);''')
edit(file, '  g_autoptr (GVariant) native_size = NULL;',
     '''  g_autoptr (GVariant) native_size = NULL;
  g_autoptr (GVariant) native_extra_variant = NULL;
  MetaNativeExtraMonitor requested_extra[META_NATIVE_MAX_EXTRA_MONITORS];
  unsigned int n_requested_extra = 0;''')
edit(file, '''                                             "Native viewport requires one protected temporary desktop");
      return G_DBUS_METHOD_INVOCATION_HANDLED;
    }
''', '''                                             "Native viewport requires one protected temporary desktop");
      return G_DBUS_METHOD_INVOCATION_HANDLED;
    }

  native_extra_variant = g_variant_lookup_value (properties_variant,
                                                 "native-takeover-extra", NULL);
  if (native_extra_variant)
    {
      GVariantIter extra_iter;
      gint32 extra_x, extra_y;
      guint32 extra_width, extra_height;

      if (!native_size ||
          !g_variant_is_of_type (native_extra_variant, G_VARIANT_TYPE ("a(iiuu)")) ||
          g_variant_n_children (native_extra_variant) > META_NATIVE_MAX_EXTRA_MONITORS)
        {
          g_dbus_method_invocation_return_error (invocation, G_DBUS_ERROR,
                                                 G_DBUS_ERROR_INVALID_ARGS,
                                                 "Invalid native remote monitor placement");
          return G_DBUS_METHOD_INVOCATION_HANDLED;
        }

      g_variant_iter_init (&extra_iter, native_extra_variant);
      while (g_variant_iter_next (&extra_iter, "(iiuu)", &extra_x, &extra_y,
                                  &extra_width, &extra_height))
        {
          if (extra_width < 200 || extra_width > 8192 ||
              extra_height < 200 || extra_height > 8192 ||
              extra_x < -65536 || extra_x > 65536 ||
              extra_y < -65536 || extra_y > 65536)
            {
              g_dbus_method_invocation_return_error (invocation, G_DBUS_ERROR,
                                                     G_DBUS_ERROR_INVALID_ARGS,
                                                     "Invalid native remote monitor placement");
              return G_DBUS_METHOD_INVOCATION_HANDLED;
            }
          requested_extra[n_requested_extra++] = (MetaNativeExtraMonitor) {
            .x = extra_x, .y = extra_y,
            .width = (int) extra_width, .height = (int) extra_height,
          };
        }
    }
''')
edit(file, '''                                          META_MONITORS_CONFIG_FLAG_NONE);
  if (!meta_verify_monitors_config (config, manager, &error))''',
     '''                                          META_MONITORS_CONFIG_FLAG_NONE);
  if (native_size)
    {
      MetaMonitorsConfig *extended =
        meta_monitor_config_manager_native_extend (manager, config,
                                                   requested_extra,
                                                   n_requested_extra);
      if (extended)
        {
          g_object_unref (config);
          config = extended;
        }
    }
  if (!meta_verify_monitors_config (config, manager, &error))''')
edit(file, '''  if (method == META_MONITORS_CONFIG_METHOD_PERSISTENT)
    request_persistent_confirmation (manager);''',
     '''  if (method != META_MONITORS_CONFIG_METHOD_VERIFY)
    {
      /* Placement hints belong only to the viewport they were applied with. */
      n_native_extra = native_size ? n_requested_extra : 0;
      memcpy (native_extra, requested_extra,
              n_native_extra * sizeof (MetaNativeExtraMonitor));
    }

  if (method == META_MONITORS_CONFIG_METHOD_PERSISTENT)
    request_persistent_confirmation (manager);''')

# Upstream monitor streams record a cursor-only frame on every prepare-frame
# of any other view. A remote-only monitor repaints continuously, so those
# frames kept the viewport's full frames permanently "too early". Drop
# unchanged cursor metadata, as the virtual/window/area sources already do.
file = 'src/backends/meta-screen-cast-monitor-stream-src.c'
edit(file, '''static void
meta_screen_cast_monitor_stream_src_set_cursor_metadata (MetaScreenCastStreamSrc *src,''',
     '''static gboolean
meta_screen_cast_monitor_stream_src_is_cursor_metadata_valid (MetaScreenCastStreamSrc *src)
{
  MetaScreenCastMonitorStreamSrc *monitor_src =
    META_SCREEN_CAST_MONITOR_STREAM_SRC (src);

  if (!g_getenv ("MUTTER_NATIVE_TAKEOVER"))
    return FALSE;

  if (should_cursor_metadata_be_set (monitor_src))
    {
      int x, y;

      if (!monitor_src->last_cursor_matadata.set ||
          monitor_src->cursor_bitmap_invalid)
        return FALSE;

      get_cursor_position (monitor_src, &x, &y);

      return (monitor_src->last_cursor_matadata.x == x &&
              monitor_src->last_cursor_matadata.y == y);
    }

  return !monitor_src->last_cursor_matadata.set;
}

static void
meta_screen_cast_monitor_stream_src_set_cursor_metadata (MetaScreenCastStreamSrc *src,''')
edit(file, '''  src_class->set_cursor_metadata =
    meta_screen_cast_monitor_stream_src_set_cursor_metadata;''',
     '''  src_class->is_cursor_metadata_valid =
    meta_screen_cast_monitor_stream_src_is_cursor_metadata_valid;
  src_class->set_cursor_metadata =
    meta_screen_cast_monitor_stream_src_set_cursor_metadata;''')

# The leased GRD tags each extra stream with its client slot. Only tagged
# virtual monitors are remote-only; the tag may arrive after creation.
file = 'src/backends/meta-screen-cast-virtual-stream-src.c'
edit(file, '#include "config.h"', '#include "config.h"\n#include "backends/meta-monitor-config-manager.h"')
edit(file, '  gboolean has_preferred_scale;', '  gboolean has_preferred_scale;\n  int native_remote_slot_plus_one;')
edit(file, 'static char *\ngenerate_next_virtual_monitor_serial (void)', """static void
sync_native_remote_slot (MetaScreenCastVirtualStreamSrc *virtual_src,
                         gboolean                        present)
{
  MetaOutput *output;

  if (!virtual_src->virtual_monitor || !virtual_src->native_remote_slot_plus_one)
    return;

  output = meta_virtual_monitor_get_output (virtual_src->virtual_monitor);
  meta_monitor_set_native_remote_slot (meta_output_get_name (output),
                                       present ? virtual_src->native_remote_slot_plus_one - 1 : -1);
}

static char *
generate_next_virtual_monitor_serial (void)""")
edit(file, """  virtual_src->virtual_monitor = virtual_monitor;

  meta_monitor_manager_reload (monitor_manager);
}""", """  virtual_src->virtual_monitor = virtual_monitor;
  sync_native_remote_slot (virtual_src, TRUE);

  meta_monitor_manager_reload (monitor_manager);
}""")
edit(file, """      virtual_src->virtual_monitor = virtual_monitor;
      meta_monitor_manager_reload (monitor_manager);""", """      virtual_src->virtual_monitor = virtual_monitor;
      sync_native_remote_slot (virtual_src, TRUE);
      meta_monitor_manager_reload (monitor_manager);""")
edit(file, '  g_clear_object (&virtual_src->virtual_monitor);', '  sync_native_remote_slot (virtual_src, FALSE);\n  g_clear_object (&virtual_src->virtual_monitor);')
edit(file, """      virtual_src->has_preferred_scale = TRUE;
      virtual_src->preferred_scale = (float) scale;
    }
}""", """      virtual_src->has_preferred_scale = TRUE;
      virtual_src->preferred_scale = (float) scale;
    }
  else if (g_strcmp0 (key, "org.gnome.native-takeover.remote-slot") == 0 &&
           g_getenv ("MUTTER_NATIVE_TAKEOVER"))
    {
      MetaScreenCastVirtualStreamSrc *virtual_src =
        META_SCREEN_CAST_VIRTUAL_STREAM_SRC (src);
      gint64 slot = g_ascii_strtoll (value, NULL, 10);

      if (slot < 0 || slot >= META_NATIVE_MAX_EXTRA_MONITORS ||
          virtual_src->native_remote_slot_plus_one == slot + 1)
        return;

      virtual_src->native_remote_slot_plus_one = (int) slot + 1;
      if (virtual_src->virtual_monitor)
        {
          sync_native_remote_slot (virtual_src, TRUE);
          meta_monitor_manager_reload (
            meta_backend_get_monitor_manager (backend_from_src (src)));
        }
    }
}""")

manifest = root / 'build/native-patch-hashes.json'
previous = json.loads(manifest.read_text()) if manifest.exists() else {}
digest = lambda s: hashlib.sha256(s.encode()).hexdigest()
patch = []
for name, modified in changes.items():
    target = source / name
    current = target.read_text() if target.exists() else ''
    if current not in (originals[name], modified) and digest(current) != previous.get(name):
        raise SystemExit(f'Unrecognized source edits: {name}')
for name, modified in changes.items():
    target = source / name
    if not target.exists() or target.read_text() != modified:
        target.write_text(modified)
    patch.extend(difflib.unified_diff(originals[name].splitlines(True), modified.splitlines(True),
                                    fromfile='a/' + name if originals[name] else '/dev/null', tofile='b/' + name))
manifest.parent.mkdir(exist_ok=True)
manifest.write_text(json.dumps({n: digest(s) for n, s in changes.items()}, indent=2) + '\n')
(root / 'patches/mutter-native-takeover.patch').write_text(''.join(patch))
print(f'Patched {len(changes)} Mutter files at {revision}')
