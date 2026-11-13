/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once
#include "meta/meta-context.h"
#include "clutter/clutter.h"
struct libinput_event;
struct _MetaLogicalMonitor;
gboolean meta_native_privacy_get_viewport_size (struct _MetaLogicalMonitor *monitor,
                                                int *width, int *height);
/* FALSE only for an extra remote-only monitor beside an active viewport.
 * Unknown views, and every view without a viewport, are physical. */
gboolean meta_native_privacy_view_is_physical (ClutterStageView *view);
void meta_native_privacy_init (MetaContext *context);
gboolean meta_native_privacy_active (void);
gboolean meta_native_privacy_has_lease (void);
gboolean meta_native_privacy_filter_input (struct libinput_event *event);
void meta_native_privacy_painted (ClutterStageView *view, ClutterFrame *frame);
void meta_native_privacy_paint_local (ClutterStageView *view);

guint meta_native_privacy_key_count (guint key, guint fallback);
gboolean meta_native_privacy_is_cleanup_event (const ClutterEvent *event);
