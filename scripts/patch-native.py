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
  if (meta_native_privacy_active ())
    clutter_stage_view_add_redraw_clip (view, NULL);

  notify_watchers_for_mode (meta_stage, view, NULL, frame,
                            META_STAGE_WATCH_BEFORE_PAINT);''')
edit('src/backends/meta-stage.c', '  CLUTTER_ACTOR_CLASS (meta_stage_parent_class)->paint (actor, paint_context);', '''  gboolean physical_private;

  /* Offscreen PipeWire repaint has no stage view and sees the original scene.
   * Never attach an actor overlay: that would also blacken remote capture. */
  physical_private = meta_native_privacy_active () &&
                     clutter_paint_context_get_stage_view (paint_context) != NULL;
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
  if (meta_native_privacy_active ())
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
      if (!meta_native_privacy_has_lease () ||
          logical_monitor_config->layout.x != 0 ||
          logical_monitor_config->layout.y != 0 ||
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
      if (current && current->logical_monitor_configs)
        {
          MetaLogicalMonitorConfig *logical = current->logical_monitor_configs->data;
          if (logical->native_takeover_viewport)
            g_variant_builder_add (&properties_builder, "{sv}",
                                   "native-takeover-size",
                                   g_variant_new ("(uu)", logical->native_viewport_width,
                                                  logical->native_viewport_height));
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

  if (!clutter_stage_paint_to_buffer (stage, &logical_monitor->rect, scale,''')

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
