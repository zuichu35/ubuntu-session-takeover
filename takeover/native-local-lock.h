/* SPDX-License-Identifier: GPL-2.0-or-later */
#pragma once
#include "clutter/clutter.h"
#include <xkbcommon/xkbcommon.h>

void meta_native_local_lock_begin (ClutterActor *stage, struct xkb_keymap *keymap,
                                   xkb_layout_index_t group, gboolean caps_lock,
                                   gboolean num_lock, gboolean test_mode,
                                   void (*verified) (void));
void meta_native_local_lock_end (void);
void meta_native_local_lock_set_recovery (gboolean recovery);
void meta_native_local_lock_key (guint key, gboolean down, gboolean fresh);
void meta_native_local_lock_paint (ClutterStageView *view);
