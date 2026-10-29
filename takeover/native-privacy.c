/* SPDX-License-Identifier: GPL-2.0-or-later
 * Native seat privacy for an authenticated RDP lease. The actual GNOME session
 * retains its DRM, libinput and Wayland clients throughout the handover.
 * Screen capture paints offscreen; only physical stage views are blackened.
 */
#include "config.h"
#include "backends/meta-native-privacy.h"
#include "backends/meta-native-local-lock.h"
#include "backends/meta-backend-private.h"
#include "backends/meta-monitor-config-manager.h"
#include "backends/meta-monitor-manager-private.h"
#include "clutter/clutter-mutter.h"
#include "meta/compositor.h"
#include "meta/meta-remote-access-controller.h"
#include <libinput.h>
#include <linux/input-event-codes.h>
#include <unistd.h>
#include <pwd.h>
#include <grp.h>
#include <gmodule.h>
#include <cairo.h>
#include "backends/native/meta-seat-native.h"
#include "backends/native/meta-input-thread.h"
#include "backends/native/meta-input-device-native.h"
#include "clutter/clutter-event-private.h"
#include "clutter/clutter-stage-private.h"

static MetaContext *privacy_context;
static GDBusConnection *privacy_bus;
static GDBusConnection *system_bus;
static GDBusMethodInvocation *begin_call;
static char *owner;
static char *session_path;
static guint owner_watch;
static guint timeout_id;
static GHashTable *painted_views;
static GHashTable *presented_views;
static gint active;
static gint startup_guard = -1;
static gboolean greeter_mode, greeter_shell_uninhibited, startup_guard_ready;
static gboolean startup_failed, startup_local_ui, local_authenticated;
static gint64 startup_started, reclaim_started, last_unlock_request;
static gint64 startup_timeout_us = 30 * G_USEC_PER_SEC;
static gint64 reclaim_timeout_us = 10 * G_USEC_PER_SEC;
static guint lock_epoch, lock_retries;
static GCancellable *lock_cancel;
static gboolean saver_known, saver_active, hint_known, hint_locked;
static const char *lock_reason = "idle";
static char *greeter_session_path;
static gint enabled;
static gboolean locking;
static gboolean remote_inhibited;
static gboolean lock_poll_pending;
static gboolean lock_request_pending;
static gint64 last_lock_request;
static gboolean unlock_pending;
static gboolean unlock_requested;
static gboolean test_mode;
static gboolean reclaiming;
static gint lease_generation;
static GMutex input_mutex;
static guint physical_keys[KEY_CNT];
typedef struct {
  ClutterInputDevice *test_device; /* Only populated in headless fault-injection tests. */
  gboolean keys[KEY_CNT];
  gboolean blocked_keys[KEY_CNT];
  gboolean auth_keys[KEY_CNT];
  enum libinput_event_type delivered_keys[KEY_CNT];
  GHashTable *touch_slots;
  gboolean blocked_touch, blocked_tip, blocked_gesture;
  gboolean delivered_tip;
  enum libinput_event_type delivered_gesture;
  guint gesture_fingers;
  guint contacts;
  gboolean tip;
  gboolean gesture;
} PhysicalDevice;
static GHashTable *physical_devices;
static guint physical_down;
static gboolean input_reset_pending;

static const char xml[] =
  "<node><interface name='org.gnome.Mutter.NativeTakeover'>"
  "<method name='Begin'><arg name='session' type='o' direction='in'/></method>"
  "<method name='End'/><method name='GetState'>"
  "<arg type='s' direction='out'/></method>"
  "<method name='GetStatus'><arg type='a{sv}' direction='out'/></method>"
  "<method name='InspectPhysical'><arg type='u' direction='out'/>"
  "<arg type='b' direction='out'/></method>"
  "<method name='PrepareLogin'><arg type='o' direction='in'/></method>"
  "<method name='TestHoldInput'/>"
  "<method name='TestLocalKey'><arg type='u' direction='in'/><arg type='b' direction='in'/></method>"
  "<method name='TestInputState'><arg type='u' direction='out'/><arg type='u' direction='out'/><arg type='b' direction='out'/></method>"
  "<signal name='Revoked'/></interface></node>";

static MetaBackend * backend (void) { return meta_context_get_backend (privacy_context); }
static ClutterStage * stage (void) { return CLUTTER_STAGE (meta_backend_get_stage (backend ())); }
static MetaRemoteAccessController * controller (void)
{ return meta_backend_get_remote_access_controller (backend ()); }
static MetaCompositor * compositor (void)
{ return meta_display_get_compositor (meta_context_get_display (privacy_context)); }

static gboolean
startup_privacy_active (void)
{
  if (g_atomic_int_get (&startup_guard) < 0)
    g_atomic_int_compare_and_exchange (&startup_guard, -1,
      g_strcmp0 (g_getenv ("MUTTER_NATIVE_TAKEOVER"), "1") == 0 &&
      g_strcmp0 (g_getenv ("MUTTER_NATIVE_BOOT_PRIVACY"), "1") == 0);
  return g_atomic_int_get (&startup_guard);
}

gboolean meta_native_privacy_has_lease (void)
{ return g_atomic_int_get (&active) && !locking; }

gboolean meta_native_privacy_active (void)
/* Begin sets active before clearing startup_guard: read in reverse order
 * so the input thread cannot observe a gap while ownership is transferred. */
{ return startup_privacy_active () || g_atomic_int_get (&active); }

gboolean
meta_native_privacy_get_viewport_size (MetaLogicalMonitor *monitor,
                                       int *width, int *height)
{
  if (!privacy_context || !meta_native_privacy_active ()) return FALSE;
  MetaMonitorManager *manager = meta_backend_get_monitor_manager (backend ());
  if (monitor != meta_monitor_manager_get_primary_logical_monitor (manager)) return FALSE;
  MetaMonitorsConfig *config = meta_monitor_config_manager_get_current (
    meta_monitor_manager_get_config_manager (manager));
  if (!config || !config->logical_monitor_configs) return FALSE;
  MetaLogicalMonitorConfig *logical = config->logical_monitor_configs->data;
  if (!logical->native_takeover_viewport) return FALSE;
  *width = logical->native_viewport_width;
  *height = logical->native_viewport_height;
  return TRUE;
}

static const char * state (void)
{
  if (startup_privacy_active ())
    {
      if (startup_failed) return "startup-failed";
      if (locking || unlock_pending) return "startup-locking";
      return startup_guard_ready ? "ready-guarded" : "boot-guard";
    }
  if (locking) return "locking";
  if (begin_call) return "blanking";
  return meta_native_privacy_active () ? "remote" : "local";
}

static void start_lock (void);
static void unlock_reply (GObject *source, GAsyncResult *result, gpointer data);

typedef struct { guint key; gboolean down; gint generation; } LocalKey;

static gboolean
deliver_local_key (gpointer data)
{
  LocalKey *event = data;
  if (greeter_mode && event->key == KEY_ESC && event->down &&
      meta_native_privacy_active () && !locking && !begin_call)
    { start_lock (); return G_SOURCE_REMOVE; }
  if (event->generation == g_atomic_int_get (&lease_generation) &&
      meta_native_privacy_active () && (!locking || startup_local_ui) &&
      !begin_call && !reclaiming)
    meta_native_local_lock_key (event->key, event->down, TRUE);
  return G_SOURCE_REMOVE;
}

static void
queue_local_key (guint key, gboolean down)
{
  LocalKey *event = g_new0 (LocalKey, 1);
  event->key = key; event->down = down;
  event->generation = g_atomic_int_get (&lease_generation);
  g_idle_add_full (G_PRIORITY_DEFAULT, deliver_local_key, event, g_free);
}

static void local_verified (void);

void
meta_native_privacy_paint_local (ClutterStageView *view)
{
  if (meta_native_privacy_active () && (!locking || startup_local_ui || reclaiming) && !begin_call)
    meta_native_local_lock_paint (view);
}

/* This runs in the libinput thread BEFORE modifier state, repeat, keybindings,
 * accessibility, or application dispatch. EI virtual input does not enter it.
 * Track physical and delivered state separately. A held physical input never
 * blocks a lease; old presses are quarantined individually until released.
 */
static void
physical_device_free (PhysicalDevice *device)
{
  g_clear_pointer (&device->touch_slots, g_hash_table_unref);
  g_clear_object (&device->test_device);
  g_free (device);
}

guint
meta_native_privacy_key_count (guint key, guint fallback)
{
  GHashTableIter iter;
  gpointer value;
  guint count = 0;
  if (!g_atomic_int_get (&enabled)) return fallback;
  g_mutex_lock (&input_mutex);
  if (physical_devices)
    {
      g_hash_table_iter_init (&iter, physical_devices);
      while (g_hash_table_iter_next (&iter, NULL, &value))
        count += ((PhysicalDevice *) value)->delivered_keys[key] != 0;
    }
  g_mutex_unlock (&input_mutex);
  return count;
}

gboolean
meta_native_privacy_filter_input (struct libinput_event *event)
{
  enum libinput_event_type type;
  gboolean drop, down = FALSE, remote;
  guint key = KEY_CNT;
  PhysicalDevice *device;
  struct libinput_device *input_device;
  if (!g_atomic_int_get (&enabled)) return FALSE;
  type = libinput_event_get_type (event);
  g_mutex_lock (&input_mutex);
  input_device = libinput_event_get_device (event);
  if (!physical_devices)
    physical_devices = g_hash_table_new_full (g_direct_hash, g_direct_equal, NULL, (GDestroyNotify) physical_device_free);
  device = g_hash_table_lookup (physical_devices, input_device);
  if (!device)
    {
      device = g_new0 (PhysicalDevice, 1);
      g_hash_table_insert (physical_devices, input_device, device);
    }
  if (type == LIBINPUT_EVENT_DEVICE_REMOVED)
    {
      for (guint i = 0; i < KEY_CNT; i++)
        {
          if (device->keys[i]) { physical_keys[i]--; physical_down--; }
          if (device->auth_keys[i]) queue_local_key (i, FALSE);
        }
      physical_down -= device->contacts + device->tip + device->gesture;
      g_hash_table_remove (physical_devices, input_device);
    }
  if (type == LIBINPUT_EVENT_DEVICE_ADDED || type == LIBINPUT_EVENT_DEVICE_REMOVED)
    { g_mutex_unlock (&input_mutex); return FALSE; }
  if (type == LIBINPUT_EVENT_KEYBOARD_KEY)
    {
      struct libinput_event_keyboard *k = libinput_event_get_keyboard_event (event);
      key = libinput_event_keyboard_get_key (k);
      down = libinput_event_keyboard_get_key_state (k) == LIBINPUT_KEY_STATE_PRESSED;
    }
  else if (type == LIBINPUT_EVENT_POINTER_BUTTON)
    {
      struct libinput_event_pointer *p = libinput_event_get_pointer_event (event);
      key = libinput_event_pointer_get_button (p);
      down = libinput_event_pointer_get_button_state (p) == LIBINPUT_BUTTON_STATE_PRESSED;
    }
  else if (type == LIBINPUT_EVENT_TABLET_TOOL_BUTTON)
    {
      struct libinput_event_tablet_tool *t = libinput_event_get_tablet_tool_event (event);
      key = libinput_event_tablet_tool_get_button (t);
      down = libinput_event_tablet_tool_get_button_state (t) == LIBINPUT_BUTTON_STATE_PRESSED;
    }
  remote = meta_native_privacy_active ();
  drop = remote;
  if (key < KEY_CNT)
    {
      drop |= device->blocked_keys[key];
      if (device->keys[key] != down)
        {
          if (remote && type == LIBINPUT_EVENT_KEYBOARD_KEY)
            {
              if (down && !device->blocked_keys[key])
                { device->auth_keys[key] = TRUE; queue_local_key (key, TRUE); }
              else if (!down && device->auth_keys[key])
                { device->auth_keys[key] = FALSE; queue_local_key (key, FALSE); }
            }
          device->keys[key] = down;
          if (down) { physical_down++; physical_keys[key]++; }
          else { physical_down--; physical_keys[key]--; }
        }
      if (remote && down) device->blocked_keys[key] = TRUE;
      if (!down) device->blocked_keys[key] = FALSE;
      if (!drop) device->delivered_keys[key] = down ? type : 0;
    }
  if (type >= LIBINPUT_EVENT_TOUCH_DOWN && type <= LIBINPUT_EVENT_TOUCH_FRAME)
    {
      drop |= device->blocked_touch;
      if (type == LIBINPUT_EVENT_TOUCH_DOWN) { physical_down++; device->contacts++; }
      if (type == LIBINPUT_EVENT_TOUCH_UP && device->contacts) { physical_down--; device->contacts--; }
      if (type == LIBINPUT_EVENT_TOUCH_CANCEL) { physical_down -= device->contacts; device->contacts = 0; }
      if (remote && device->contacts) device->blocked_touch = TRUE;
      if (!device->contacts) device->blocked_touch = FALSE;
      if (!drop && type != LIBINPUT_EVENT_TOUCH_FRAME)
        {
          int slot = libinput_event_touch_get_seat_slot (libinput_event_get_touch_event (event));
          if (!device->touch_slots) device->touch_slots = g_hash_table_new (NULL, NULL);
          if (type == LIBINPUT_EVENT_TOUCH_DOWN)
            g_hash_table_add (device->touch_slots, GINT_TO_POINTER (slot + 1));
          else if (type == LIBINPUT_EVENT_TOUCH_UP || type == LIBINPUT_EVENT_TOUCH_CANCEL)
            g_hash_table_remove (device->touch_slots, GINT_TO_POINTER (slot + 1));
        }
    }
  if (type == LIBINPUT_EVENT_TABLET_TOOL_AXIS || type == LIBINPUT_EVENT_TABLET_TOOL_TIP)
    drop |= device->blocked_tip;
  if (type == LIBINPUT_EVENT_TABLET_TOOL_TIP)
    {
      gboolean tip = libinput_event_tablet_tool_get_tip_state (
                      libinput_event_get_tablet_tool_event (event)) == LIBINPUT_TABLET_TOOL_TIP_DOWN;
      if (device->tip != tip) { if (tip) physical_down++; else physical_down--; device->tip = tip; }
      if (remote && tip) device->blocked_tip = TRUE;
      if (!tip) device->blocked_tip = FALSE;
      if (!drop) device->delivered_tip = tip;
    }
  if (type >= LIBINPUT_EVENT_GESTURE_SWIPE_BEGIN && type <= LIBINPUT_EVENT_GESTURE_HOLD_END)
    {
      drop |= device->blocked_gesture;
      if (type == LIBINPUT_EVENT_GESTURE_SWIPE_BEGIN || type == LIBINPUT_EVENT_GESTURE_PINCH_BEGIN ||
          type == LIBINPUT_EVENT_GESTURE_HOLD_BEGIN)
        {
          if (!device->gesture) { device->gesture = TRUE; physical_down++; }
          if (remote) device->blocked_gesture = TRUE;
          if (!drop)
            {
              device->delivered_gesture = type;
              device->gesture_fingers = libinput_event_gesture_get_finger_count (libinput_event_get_gesture_event (event));
            }
        }
      if (type == LIBINPUT_EVENT_GESTURE_SWIPE_END || type == LIBINPUT_EVENT_GESTURE_PINCH_END ||
          type == LIBINPUT_EVENT_GESTURE_HOLD_END)
        {
          if (device->gesture) { device->gesture = FALSE; physical_down--; }
          device->blocked_gesture = FALSE;
          if (!drop) device->delivered_gesture = 0;
        }
    }
  g_mutex_unlock (&input_mutex);
  return drop;
}

/* Releases are deliberately allowed through the main-thread privacy barrier:
 * the raw filter blocks new physical releases while remote, so these are the
 * state-neutralizing events (or releases already queued before the barrier). */
gboolean
meta_native_privacy_is_cleanup_event (const ClutterEvent *event)
{
  switch (clutter_event_type (event))
    {
    case CLUTTER_KEY_RELEASE:
    case CLUTTER_KEY_STATE:
    case CLUTTER_BUTTON_RELEASE:
    case CLUTTER_TOUCH_CANCEL:
    case CLUTTER_PROXIMITY_OUT:
      return TRUE;
    case CLUTTER_TOUCHPAD_PINCH:
    case CLUTTER_TOUCHPAD_SWIPE:
    case CLUTTER_TOUCHPAD_HOLD:
      return clutter_event_get_gesture_phase (event) == CLUTTER_TOUCHPAD_GESTURE_PHASE_CANCEL;
    default:
      return FALSE;
    }
}

static gboolean
input_reset_finished (gpointer data)
{
  input_reset_pending = FALSE;
  clutter_actor_queue_redraw (CLUTTER_ACTOR (stage ()));
  g_message ("[NativeTakeover] Physical input isolated; delivered presses released");
  return G_SOURCE_REMOVE;
}

/* Serialized with raw libinput dispatch on the input thread. No remote lease
 * is granted until these releases have been processed on the main thread and
 * every physical output has acknowledged a black frame. */
static gboolean
reset_physical_input_in_impl (gpointer data)
{
  MetaSeatImpl *seat_impl = data;
  GHashTableIter iter;
  gpointer input_device, value;
  gboolean released[KEY_CNT] = { FALSE };
  uint64_t now = g_get_monotonic_time ();

  g_mutex_lock (&input_mutex);
  if (physical_devices)
    {
      g_hash_table_iter_init (&iter, physical_devices);
      while (g_hash_table_iter_next (&iter, &input_device, &value))
        {
          PhysicalDevice *state = value;
          ClutterInputDevice *device = state->test_device ? state->test_device : libinput_device_get_user_data (input_device);
          for (guint key = 0; key < KEY_CNT; key++)
            {
              enum libinput_event_type type = state->delivered_keys[key];
              state->blocked_keys[key] = state->keys[key];
              state->delivered_keys[key] = 0;
              if (!device || !type || !seat_impl->button_count[key]) continue;
              if (type != LIBINPUT_EVENT_TABLET_TOOL_BUTTON && released[key]) continue;
              released[key] = TRUE;
              if (type == LIBINPUT_EVENT_KEYBOARD_KEY)
                meta_seat_impl_notify_key_in_impl (seat_impl, device, now, key, 0, TRUE);
              else
                meta_seat_impl_notify_button_in_impl (seat_impl, device, now, key, 0);
            }
          if (state->touch_slots)
            {
              GHashTableIter slots;
              gpointer slot;
              g_hash_table_iter_init (&slots, state->touch_slots);
              while (g_hash_table_iter_next (&slots, &slot, NULL))
                if (device)
                  meta_seat_impl_notify_touch_event_in_impl (seat_impl, device,
                    CLUTTER_TOUCH_CANCEL, now, GPOINTER_TO_INT (slot) - 1, 0, 0);
              g_hash_table_remove_all (state->touch_slots);
            }
          state->blocked_touch = state->contacts > 0;
          if (device && state->delivered_tip && seat_impl->button_count[BTN_TOUCH])
            meta_seat_impl_notify_button_in_impl (seat_impl, device, now, BTN_TOUCH, 0);
          state->delivered_tip = FALSE;
          state->blocked_tip = state->tip;
          if (device && state->delivered_gesture)
            {
              ClutterEvent *event = NULL;
              graphene_point_t zero = { 0, 0 };
              switch (state->delivered_gesture)
                {
                case LIBINPUT_EVENT_GESTURE_PINCH_BEGIN:
                  event = clutter_event_touchpad_pinch_new (CLUTTER_EVENT_NONE, now,
                    device, CLUTTER_TOUCHPAD_GESTURE_PHASE_CANCEL,
                    state->gesture_fingers, zero, zero, zero, 0, 1);
                  break;
                case LIBINPUT_EVENT_GESTURE_SWIPE_BEGIN:
                  event = clutter_event_touchpad_swipe_new (CLUTTER_EVENT_NONE, now,
                    device, CLUTTER_TOUCHPAD_GESTURE_PHASE_CANCEL,
                    state->gesture_fingers, zero, zero, zero);
                  break;
                case LIBINPUT_EVENT_GESTURE_HOLD_BEGIN:
                  event = clutter_event_touchpad_hold_new (CLUTTER_EVENT_NONE, now,
                    device, CLUTTER_TOUCHPAD_GESTURE_PHASE_CANCEL,
                    state->gesture_fingers, zero);
                  break;
                default: break;
                }
              if (event) { _clutter_event_push (event, FALSE); }
            }
          state->delivered_gesture = 0;
          state->blocked_gesture = state->gesture;
        }
    }
  g_mutex_unlock (&input_mutex);
  /* Input dispatch runs at DEFAULT priority. Complete at LOW priority so
   * queued release/cancel events drain before presentation can grant a lease. */
  g_idle_add_full (G_PRIORITY_LOW, input_reset_finished, NULL, NULL);
  return G_SOURCE_REMOVE;
}

static gboolean
reset_physical_input (gpointer task)
{
  reset_physical_input_in_impl (g_task_get_source_object (task));
  g_task_return_boolean (task, TRUE);
  return G_SOURCE_REMOVE;
}

static void
reset_lock_observation (void)
{
  lock_epoch++;
  if (lock_cancel) g_cancellable_cancel (lock_cancel);
  g_clear_object (&lock_cancel);
  lock_cancel = g_cancellable_new ();
  lock_poll_pending = FALSE;
  lock_request_pending = FALSE;
  unlock_requested = FALSE;
  saver_known = hint_known = FALSE;
}

static void
show_local_surface (gboolean startup)
{
  if (!startup_local_ui)
    {
      ClutterKeymap *keymap = clutter_seat_get_keymap (meta_backend_get_default_seat (backend ()));
      meta_native_local_lock_begin (CLUTTER_ACTOR (stage ()), meta_backend_get_xkb_keymap (backend ()),
        meta_backend_get_keymap_layout_group (backend ()), clutter_keymap_get_caps_lock_state (keymap),
        clutter_keymap_get_num_lock_state (keymap), test_mode, local_verified);
      if (startup) startup_local_ui = TRUE;
    }
  meta_native_local_lock_set_recovery (startup);
  clutter_actor_queue_redraw (CLUTTER_ACTOR (stage ()));
}

static void
finish_lock (void)
{
  if (startup_privacy_active () && !g_atomic_int_get (&active) && !reclaiming)
    {
      gboolean was_ready = startup_guard_ready;
      locking = FALSE;
      startup_guard_ready = TRUE;
      startup_failed = FALSE;
      lock_reason = "dual-lock-confirmed";
      show_local_surface (TRUE);
      if (!was_ready)
        g_message ("[NativeTakeover] Startup locked; holding physical privacy for authenticated takeover");
      /* Keep observing until handover: GDM may still send a late Unlock. */
      return;
    }
  if (timeout_id) { g_source_remove (timeout_id); timeout_id = 0; }
  reset_lock_observation ();
  if (owner_watch) { g_bus_unwatch_name (owner_watch); owner_watch = 0; }
  meta_native_local_lock_end ();
  g_atomic_int_inc (&lease_generation);
  g_clear_pointer (&owner, g_free);
  g_hash_table_remove_all (presented_views);
  g_hash_table_remove_all (painted_views);
  g_mutex_lock (&input_mutex);
  g_atomic_int_set (&active, 0);
  g_atomic_int_set (&startup_guard, 0);
  startup_guard_ready = startup_failed = startup_local_ui = FALSE;
  g_mutex_unlock (&input_mutex);
  clutter_stage_set_offscreen_picking (stage (), FALSE);
  locking = FALSE;
  unlock_pending = FALSE;
  gboolean authenticated = reclaiming;
  reclaiming = FALSE;
  startup_started = reclaim_started = 0;
  if (!authenticated) lock_reason = "locked-local";
  else if (!g_str_equal (lock_reason, "local-authenticated-shell-timeout"))
    lock_reason = "local-authenticated";
  meta_backend_uninhibit_hw_cursor (backend ());
  meta_compositor_enable_unredirect (compositor ());
  clutter_actor_queue_redraw (CLUTTER_ACTOR (stage ()));
  /* Our inhibition remains held. Local PAM alone never restores remote access. */
  g_message ("[NativeTakeover] Physical seat restored after %s",
             authenticated ? "local password authentication" : "GNOME lock confirmation");
}

static void
lock_request_done (GObject *source, GAsyncResult *result, gpointer data)
{
  g_autoptr (GVariant) reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, NULL);
  if (GPOINTER_TO_UINT (data) != lock_epoch) return;
  lock_request_pending = FALSE;
  last_lock_request = g_get_monotonic_time ();
  if (!reply && !startup_failed) lock_reason = "lock-request-unanswered";
}

static void
request_lock (void)
{
  if (lock_request_pending || reclaiming || unlock_pending) return;
  lock_request_pending = TRUE;
  last_lock_request = g_get_monotonic_time ();
  lock_retries++;
  g_dbus_connection_call (privacy_bus, "org.gnome.ScreenSaver", "/org/gnome/ScreenSaver",
                         "org.gnome.ScreenSaver", "Lock", NULL, NULL,
                         G_DBUS_CALL_FLAGS_NONE, 5000, lock_cancel, lock_request_done,
                         GUINT_TO_POINTER (lock_epoch));
}

static void
request_unlock (void)
{
  if (unlock_requested) return;
  unlock_requested = TRUE;
  last_unlock_request = g_get_monotonic_time ();
  g_dbus_connection_call (privacy_bus, "org.gnome.ScreenSaver", "/org/gnome/ScreenSaver",
                         "org.gnome.ScreenSaver", "SetActive", g_variant_new ("(b)", FALSE), NULL,
                         G_DBUS_CALL_FLAGS_NONE, 1500, lock_cancel, unlock_reply,
                         GUINT_TO_POINTER (lock_epoch));
}

typedef struct {
  guint epoch;
  guint remaining;
  gboolean saver_known, saver_active, hint_known, hint_locked;
} LockObservation;

static void
apply_lock_observation (LockObservation *observation)
{
  gint64 now = g_get_monotonic_time ();
  saver_known = observation->saver_known;
  saver_active = observation->saver_active;
  hint_known = observation->hint_known;
  hint_locked = observation->hint_locked;
  gboolean both_locked = saver_known && saver_active && hint_known && hint_locked;
  gboolean both_unlocked = saver_known && !saver_active && hint_known && !hint_locked;

  if (unlock_pending)
    {
      if (both_unlocked)
        {
          unlock_pending = FALSE;
          if (reclaiming) finish_lock ();
          else if (locking) start_lock ();
          else if (begin_call)
            {
              if (remote_inhibited)
                { meta_remote_access_controller_uninhibit_remote_access (controller ()); remote_inhibited = FALSE; }
              g_dbus_method_invocation_return_value (begin_call, NULL);
              g_clear_object (&begin_call);
              show_local_surface (FALSE);
              lock_reason = "remote-authenticated";
              g_message ("[NativeTakeover] Original GNOME desktop unlocked behind physical privacy");
            }
          return;
        }
      if (!unlock_requested && now - last_unlock_request > G_USEC_PER_SEC)
        request_unlock ();
      return;
    }

  if (startup_privacy_active () && startup_guard_ready && !both_locked)
    {
      startup_guard_ready = FALSE;
      locking = TRUE;
      startup_started = now;
      lock_reason = "startup-lock-confirmation-lost";
      g_message ("[NativeTakeover] Startup lock confirmation lost; retaining privacy and local authentication");
    }
  if (locking || (startup_privacy_active () && startup_failed))
    {
      if (both_locked && !input_reset_pending) finish_lock ();
      else if (!lock_request_pending &&
               now - last_lock_request > (startup_failed ? 5 : 1) * G_USEC_PER_SEC)
        {
          /* Lock may precede GNOME's asynchronous logind proxy. Repeating the
           * real Lock resynchronizes LockedHint even when GetActive is true. */
          request_lock ();
        }
    }
}

static void
complete_lock_observation (LockObservation *observation)
{
  if (--observation->remaining) return;
  if (observation->epoch == lock_epoch)
    {
      lock_poll_pending = FALSE;
      apply_lock_observation (observation);
    }
  g_free (observation);
}

static void
active_reply (GObject *source, GAsyncResult *result, gpointer data)
{
  LockObservation *observation = data;
  g_autoptr (GVariant) reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, NULL);
  if (reply)
    {
      observation->saver_known = TRUE;
      g_variant_get (reply, "(b)", &observation->saver_active);
    }
  complete_lock_observation (observation);
}

static void
hint_reply (GObject *source, GAsyncResult *result, gpointer data)
{
  LockObservation *observation = data;
  g_autoptr (GVariant) reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, NULL);
  g_autoptr (GVariant) value = NULL;
  if (reply)
    {
      g_variant_get (reply, "(v)", &value);
      if (g_variant_is_of_type (value, G_VARIANT_TYPE_BOOLEAN))
        {
          observation->hint_known = TRUE;
          observation->hint_locked = g_variant_get_boolean (value);
        }
    }
  complete_lock_observation (observation);
}

static gboolean
poll_lock (gpointer data)
{
  gint64 now = g_get_monotonic_time ();
  if (reclaiming && reclaim_started && now - reclaim_started >= reclaim_timeout_us)
    {
      /* PAM has authenticated the physical user and controller inhibition has
       * synchronously closed all capture/input sessions. A broken ScreenShield
       * must not strand that user behind our own black/input barrier. */
      if (!local_authenticated || !remote_inhibited) return G_SOURCE_CONTINUE;
      lock_reason = "local-authenticated-shell-timeout";
      g_warning ("[NativeTakeover] Shell unlock did not confirm; restoring authenticated local access with remote access inhibited");
      finish_lock ();
      return G_SOURCE_REMOVE;
    }
  if (startup_privacy_active () && !startup_guard_ready && !startup_failed &&
      startup_started && !reclaiming && now - startup_started >= startup_timeout_us)
    {
      startup_failed = TRUE;
      locking = FALSE;
      lock_reason = "startup-lock-timeout";
      show_local_surface (TRUE);
      g_warning ("[NativeTakeover] Startup lock timed out (ScreenSaver known=%d active=%d; logind known=%d locked=%d); privacy held, local password recovery available",
                 saver_known, saver_active, hint_known, hint_locked);
    }
  if (!locking && !unlock_pending && !startup_privacy_active ())
    { timeout_id = 0; return G_SOURCE_REMOVE; }
  if (!lock_poll_pending)
    {
      LockObservation *observation = g_new0 (LockObservation, 1);
      observation->epoch = lock_epoch;
      observation->remaining = 2;
      lock_poll_pending = TRUE;
      g_dbus_connection_call (privacy_bus, "org.gnome.ScreenSaver", "/org/gnome/ScreenSaver",
                             "org.gnome.ScreenSaver", "GetActive", NULL, G_VARIANT_TYPE ("(b)"),
                             G_DBUS_CALL_FLAGS_NONE, 1500, lock_cancel, active_reply, observation);
      /* Headless fault injection uses a real independent fake logind property
       * on the private test bus. It never replaces confirmation with TRUE. */
      GDBusConnection *bus = test_mode ? privacy_bus : system_bus;
      if (bus && session_path)
        g_dbus_connection_call (bus, "org.freedesktop.login1", session_path,
          "org.freedesktop.DBus.Properties", "Get",
          g_variant_new ("(ss)", "org.freedesktop.login1.Session", "LockedHint"),
          G_VARIANT_TYPE ("(v)"), G_DBUS_CALL_FLAGS_NONE, 1500, lock_cancel, hint_reply, observation);
      else
        complete_lock_observation (observation);
    }
  return G_SOURCE_CONTINUE;
}

static void
start_lock (void)
{
  if (!meta_native_privacy_active ()) return;
  if (reclaiming) return; /* Already revoked after successful local authentication. */
  if (!startup_local_ui) meta_native_local_lock_end ();
  gboolean pending_unlock_request = unlock_pending && unlock_requested;
  reset_lock_observation ();
  /* Do not duplicate a remote unlock whose asynchronous side effects may
   * still complete. Observe that request becoming unlocked before re-locking. */
  unlock_requested = pending_unlock_request;
  if (startup_privacy_active () && !startup_started)
    startup_started = g_get_monotonic_time ();
  if (begin_call)
    {
      g_dbus_method_invocation_return_dbus_error (begin_call,
        "org.gnome.Mutter.NativeTakeover.Cancelled", "Privacy handover cancelled");
      g_clear_object (&begin_call);
    }
  if (timeout_id) { g_source_remove (timeout_id); timeout_id = 0; }
  locking = TRUE;
  lock_reason = startup_privacy_active () ? "startup-lock-pending" : "lock-pending";
  clutter_actor_queue_redraw (CLUTTER_ACTOR (stage ()));
  if (!remote_inhibited)
    {
      meta_remote_access_controller_inhibit_remote_access (controller ());
      remote_inhibited = TRUE;
    }
  g_dbus_connection_emit_signal (privacy_bus, NULL, "/org/gnome/Mutter/NativeTakeover",
                                "org.gnome.Mutter.NativeTakeover", "Revoked", NULL, NULL);
  if (greeter_mode)
    {
      if (greeter_shell_uninhibited)
        {
          meta_remote_access_controller_inhibit_remote_access (controller ());
          greeter_shell_uninhibited = FALSE;
        }
      /* Returning to an actual GDM greeter cannot expose a user desktop. */
      finish_lock ();
      return;
    }
  if (!unlock_pending)
    request_lock ();
  timeout_id = g_timeout_add (100, poll_lock, NULL);
  g_message ("[NativeTakeover] Revoked; holding privacy until GNOME lock confirmation");
}

static void
local_verified (void)
{
  if (!meta_native_privacy_active () || (locking && !startup_local_ui) ||
      begin_call || reclaiming) return;
  local_authenticated = TRUE;
  reclaiming = TRUE;
  locking = TRUE;
  reclaim_started = g_get_monotonic_time ();
  reset_lock_observation ();
  if (!remote_inhibited)
    {
      meta_remote_access_controller_inhibit_remote_access (controller ());
      remote_inhibited = TRUE;
    }
  g_dbus_connection_emit_signal (privacy_bus, NULL, "/org/gnome/Mutter/NativeTakeover",
                                "org.gnome.Mutter.NativeTakeover", "Revoked", NULL, NULL);
  /* Stay behind physical privacy until any remote ScreenShield lock is also
   * deactivated. Authentication is bound to this still-current lease. */
  unlock_pending = TRUE;
  unlock_requested = FALSE;
  last_unlock_request = 0;
  lock_reason = "local-authenticated-revoking";
  if (timeout_id) g_source_remove (timeout_id);
  timeout_id = g_timeout_add (100, poll_lock, NULL);
  g_message ("[NativeTakeover] Local password verified; revoked remote before physical restore");
}

static void
owner_gone (GDBusConnection *connection, const char *name, gpointer data)
{ start_lock (); }

static void
unlock_reply (GObject *source, GAsyncResult *result, gpointer data)
{
  g_autoptr (GVariant) reply = g_dbus_connection_call_finish (G_DBUS_CONNECTION (source), result, NULL);
  if (GPOINTER_TO_UINT (data) != lock_epoch) return;
  if (reclaiming) unlock_requested = FALSE;
  last_unlock_request = g_get_monotonic_time ();
  if (!reply && unlock_pending && !reclaiming)
    {
      /* A timeout does not prove that Shell did not accept the request. Keep
       * waiting for its actual state; never expose an ambiguously unlocked UI. */
      start_lock ();
    }
}

void
meta_native_privacy_painted (ClutterStageView *view, ClutterFrame *frame)
{
  int64_t *count;
  if (!begin_call || g_hash_table_contains (painted_views, view)) return;
  count = g_new (int64_t, 1);
  *count = clutter_frame_get_count (frame);
  g_hash_table_insert (painted_views, g_object_ref (view), count);
}

static void
presented (ClutterStage *unused, ClutterStageView *view, ClutterFrameInfo *info, gpointer data)
{
  int64_t *count;
  GList *views, *l;
  if (!begin_call || unlock_pending || input_reset_pending) return;
  count = g_hash_table_lookup (painted_views, view);
  if (!count || info->view_frame_counter < *count) return;
  g_hash_table_add (presented_views, view);
  views = clutter_stage_peek_stage_views (stage ());
  if (!views) return;
  for (l = views; l; l = l->next)
    if (!g_hash_table_contains (presented_views, l->data)) return;
  if (timeout_id) { g_source_remove (timeout_id); timeout_id = 0; }
  if (greeter_mode)
    {
      if (remote_inhibited)
        { meta_remote_access_controller_uninhibit_remote_access (controller ()); remote_inhibited = FALSE; }
      /* Shell's gdm mode contributes one inhibit. Borrow it only during
       * this authenticated, physically private lease, then restore on End. */
      meta_remote_access_controller_uninhibit_remote_access (controller ());
      greeter_shell_uninhibited = TRUE;
      g_dbus_method_invocation_return_value (begin_call, NULL);
      g_clear_object (&begin_call);
      g_message ("[NativeTakeover] GDM greeter captured behind physical privacy; authentication remains with GDM");
      return;
    }
  /* Only after real presentation acknowledgements may the original session
   * unlock. Keep ownership and the black/input barrier during its async work. */
  unlock_pending = TRUE;
  unlock_requested = FALSE;
  last_unlock_request = 0;
  /* First inspect the lock state. Calling deactivate on an already unlocked
   * ScreenShield would schedule a redundant, late _completeDeactivate(). */
  timeout_id = g_timeout_add (100, poll_lock, NULL);
  g_message ("[NativeTakeover] All current outputs presented black; unlocking original scene");
}

static gboolean
blank_timeout (gpointer data)
{
  timeout_id = 0;
  start_lock ();
  return G_SOURCE_REMOVE;
}

static GVariant *
session_properties (const char *path)
{
  g_autoptr (GVariant) reply = NULL;
  GVariant *props;

  if (!system_bus) return NULL;
  reply = g_dbus_connection_call_sync (system_bus, "org.freedesktop.login1", path,
    "org.freedesktop.DBus.Properties", "GetAll",
    g_variant_new ("(s)", "org.freedesktop.login1.Session"), G_VARIANT_TYPE ("(a{sv})"),
    G_DBUS_CALL_FLAGS_NONE, 1000, NULL, NULL);
  if (!reply) return NULL;
  g_variant_get (reply, "(@a{sv})", &props);
  return props;
}

static gboolean
session_matches (GVariant   *props,
                 uid_t       expected_uid,
                 const char *expected_class)
{
  guint uid;
  gboolean remote;
  const char *user_path, *seat, *seat_path, *type, *klass, *state;

  if (props && g_variant_lookup (props, "State", "&s", &state) &&
      g_str_equal (state, "closing")) return FALSE;

  return props &&
    g_variant_lookup (props, "User", "(u&o)", &uid, &user_path) && uid == expected_uid &&
    g_variant_lookup (props, "Seat", "(&s&o)", &seat, &seat_path) && g_str_equal (seat, "seat0") &&
    g_variant_lookup (props, "Type", "&s", &type) && g_str_equal (type, "wayland") &&
    g_variant_lookup (props, "Class", "&s", &klass) &&
    g_str_equal (klass, expected_class) &&
    g_variant_lookup (props, "Remote", "b", &remote) && !remote;
}

static gboolean
is_gdm_account (uid_t uid)
{
  struct passwd *account = getpwuid (uid);
  gid_t primary_gid;
  struct group *gdm_group;

  if (!account) return FALSE;
  primary_gid = account->pw_gid;
  gdm_group = getgrnam ("gdm");
  return gdm_group && primary_gid == gdm_group->gr_gid;
}

static char *
find_greeter_session (void)
{
  g_autoptr (GVariant) reply = NULL;
  g_autoptr (GVariant) sessions = NULL;
  g_autofree char *matched = NULL;
  GVariantIter iter;
  const char *id, *name, *seat, *path;
  guint uid;

  /* GDM creates ephemeral NSS users whose primary group remains gdm. A name
   * prefix or group membership alone is not authority to capture a greeter. */
  if (!system_bus || !is_gdm_account (getuid ())) return NULL;
  reply = g_dbus_connection_call_sync (system_bus, "org.freedesktop.login1",
    "/org/freedesktop/login1", "org.freedesktop.login1.Manager", "ListSessions",
    NULL, G_VARIANT_TYPE ("(a(susso))"), G_DBUS_CALL_FLAGS_NONE, 2000, NULL, NULL);
  if (!reply) return NULL;
  g_variant_get (reply, "(@a(susso))", &sessions);
  g_variant_iter_init (&iter, sessions);
  while (g_variant_iter_loop (&iter, "(&su&s&s&o)", &id, &uid, &name, &seat, &path))
    {
      g_autoptr (GVariant) props = NULL;
      if (uid != getuid () || !g_str_equal (seat, "seat0")) continue;
      props = session_properties (path);
      if (!session_matches (props, uid, "greeter")) continue;
      if (matched) return NULL; /* Ambiguous physical sessions fail closed. */
      matched = g_strdup (path);
    }
  return g_steal_pointer (&matched);
}

static char *
find_greeter_session_with_retry (void)
{
  for (guint attempt = 0; attempt < 3; attempt++)
    {
      char *path = find_greeter_session ();
      if (path) return path;
      if (attempt < 2) g_usleep (250 * 1000);
    }
  return NULL;
}

static gboolean
validate_session (const char *path)
{
  g_autoptr (GVariant) props = NULL;

  if (test_mode) return TRUE;
  if (greeter_mode && (!is_gdm_account (getuid ()) ||
                      g_strcmp0 (path, greeter_session_path) != 0)) return FALSE;
  props = session_properties (path);
  return session_matches (props, getuid (), greeter_mode ? "greeter" : "user");
}

/* Fault injection is restricted to explicitly enabled, headless test mode.
 * Use real native-seat notifications so the integration test verifies XKB,
 * repeat and pointer-button state, not just the bookkeeping in the filter. */
static gboolean
test_input_task (gpointer data)
{
  GDBusMethodInvocation *invocation = g_task_get_task_data (data);
  MetaSeatImpl *seat_impl = g_task_get_source_object (data);
  const char *method = g_dbus_method_invocation_get_method_name (invocation);
  if (g_str_equal (method, "TestInputState"))
    {
      guint count = 0;
      for (guint k = 0; k < KEY_CNT; k++) count += seat_impl->button_count[k];
      g_dbus_method_invocation_return_value (invocation, g_variant_new ("(uub)", count,
        xkb_state_serialize_mods (seat_impl->xkb, XKB_STATE_MODS_DEPRESSED),
        seat_impl->repeat_source != NULL));
    }
  else
    {
      const guint keys[] = { KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_BRIGHTNESSDOWN,
                             KEY_BRIGHTNESSUP, KEY_MICMUTE };
      g_mutex_lock (&input_mutex);
      if (!physical_devices)
        physical_devices = g_hash_table_new_full (NULL, NULL, NULL,
                                                  (GDestroyNotify) physical_device_free);
      for (int pointer = 0; pointer < 2; pointer++)
        {
          PhysicalDevice *state = g_new0 (PhysicalDevice, 1);
          state->test_device = meta_input_device_native_new_virtual_in_impl (seat_impl,
                                pointer ? CLUTTER_POINTER_DEVICE : CLUTTER_KEYBOARD_DEVICE);
          meta_seat_impl_add_virtual_input_device (seat_impl, state->test_device);
          /* Avoid activating Shell's top-left hot corner while seeding a drag. */
          if (pointer)
            meta_seat_impl_notify_absolute_motion_in_impl (seat_impl, state->test_device,
                                                           g_get_monotonic_time (), 450, 300, NULL);
          g_hash_table_insert (physical_devices, state->test_device, state);
          for (guint i = 0; i < (pointer ? 1 : G_N_ELEMENTS (keys)); i++)
            {
              guint key = pointer ? BTN_LEFT : keys[i];
              state->keys[key] = TRUE;
              state->delivered_keys[key] = pointer ? LIBINPUT_EVENT_POINTER_BUTTON : LIBINPUT_EVENT_KEYBOARD_KEY;
              physical_keys[key]++; physical_down++;
              if (pointer)
                meta_seat_impl_notify_button_in_impl (seat_impl, state->test_device,
                                                      g_get_monotonic_time (), key, 1);
              else
                meta_seat_impl_notify_key_in_impl (seat_impl, state->test_device,
                                                   g_get_monotonic_time (), key, 1, TRUE);
            }
        }
      g_mutex_unlock (&input_mutex);
      g_dbus_method_invocation_return_value (invocation, NULL);
    }
  return G_SOURCE_REMOVE;
}

static void
method_call (GDBusConnection *connection, const char *sender, const char *path,
             const char *interface, const char *method, GVariant *params,
             GDBusMethodInvocation *invocation, gpointer data)
{
  if (test_mode && (g_str_equal (method, "TestInputState") ||
                   (g_str_equal (method, "TestHoldInput") && !meta_native_privacy_active ())))
    {
      MetaSeatNative *seat = META_SEAT_NATIVE (meta_backend_get_default_seat (backend ()));
      meta_seat_native_run_impl_task (seat, test_input_task, g_object_ref (invocation), g_object_unref);
      return;
    }
  if (g_str_equal (method, "GetState"))
    { g_dbus_method_invocation_return_value (invocation, g_variant_new ("(s)", state ())); return; }
  if (g_str_equal (method, "GetStatus"))
    {
      GVariantBuilder status;
      g_variant_builder_init (&status, G_VARIANT_TYPE ("a{sv}"));
      g_variant_builder_add (&status, "{sv}", "state", g_variant_new_string (state ()));
      g_variant_builder_add (&status, "{sv}", "local-authenticated", g_variant_new_boolean (local_authenticated));
      g_variant_builder_add (&status, "{sv}", "has-owner", g_variant_new_boolean (owner != NULL));
      g_variant_builder_add (&status, "{sv}", "remote-inhibited", g_variant_new_boolean (remote_inhibited));
      g_variant_builder_add (&status, "{sv}", "session-path", g_variant_new_string (session_path ? session_path : ""));
      g_variant_builder_add (&status, "{sv}", "screensaver-known", g_variant_new_boolean (saver_known));
      g_variant_builder_add (&status, "{sv}", "screensaver-active", g_variant_new_boolean (saver_active));
      g_variant_builder_add (&status, "{sv}", "logind-known", g_variant_new_boolean (hint_known));
      g_variant_builder_add (&status, "{sv}", "logind-locked", g_variant_new_boolean (hint_locked));
      g_variant_builder_add (&status, "{sv}", "lock-request-pending", g_variant_new_boolean (lock_request_pending));
      g_variant_builder_add (&status, "{sv}", "lock-retries", g_variant_new_uint32 (lock_retries));
      g_variant_builder_add (&status, "{sv}", "elapsed-ms", g_variant_new_uint64 (startup_started ?
        (g_get_monotonic_time () - startup_started) / 1000 : 0));
      g_variant_builder_add (&status, "{sv}", "reason", g_variant_new_string (lock_reason));
      g_dbus_method_invocation_return_value (invocation, g_variant_new ("(a{sv})", &status));
      return;
    }
  if (test_mode && g_str_equal (method, "TestLocalKey"))
    {
      guint key; gboolean down;
      g_variant_get (params, "(ub)", &key, &down);
      if (meta_native_privacy_active () && (!locking || startup_local_ui) && !begin_call && !reclaiming)
        meta_native_local_lock_key (key, down, TRUE);
      g_dbus_method_invocation_return_value (invocation, NULL);
      return;
    }
  if (g_str_equal (method, "InspectPhysical") && test_mode)
    {
      guint n = 0;
      gboolean black = TRUE;
      GList *l;
      for (l = clutter_stage_peek_stage_views (stage ()); l; l = l->next)
        {
          CoglFramebuffer *fb = clutter_stage_view_get_framebuffer (l->data);
          int w = cogl_framebuffer_get_width (fb), h = cogl_framebuffer_get_height (fb);
          g_autofree guint8 *pixels = g_malloc (w * h * 4);
          black &= cogl_framebuffer_read_pixels (fb, 0, 0, w, h, COGL_PIXEL_FORMAT_BGRA_8888_PRE, pixels);
          if (n == 0 && !begin_call && (!locking || startup_local_ui || reclaiming) && meta_native_privacy_active () &&
              g_getenv ("MUTTER_NATIVE_TEST_FRAME"))
            {
              cairo_surface_t *surface = cairo_image_surface_create_for_data (pixels,
                CAIRO_FORMAT_ARGB32, w, h, w * 4);
              cairo_surface_write_to_png (surface, g_getenv ("MUTTER_NATIVE_TEST_FRAME"));
              cairo_surface_destroy (surface);
            }
          for (int i = 0; i < w * h; i++)
            if (pixels[i*4] || pixels[i*4+1] || pixels[i*4+2]) { black = FALSE; break; }
          n++;
        }
      g_dbus_method_invocation_return_value (invocation, g_variant_new ("(ub)", n, black && n > 0));
      return;
    }
  if (g_str_equal (method, "PrepareLogin") && startup_privacy_active ())
    {
      const char *session;
      g_variant_get (params, "(&o)", &session);
      if (!greeter_mode && validate_session (session) &&
          (!session_path || g_str_equal (session_path, session)))
        {
          if (startup_started || startup_guard_ready || reclaiming)
            {
              g_dbus_method_invocation_return_value (invocation, NULL);
              return; /* Idempotent: never reset deadlines or password input. */
            }
          g_free (session_path);
          session_path = g_strdup (session);
          meta_compositor_disable_unredirect (compositor ());
          meta_backend_inhibit_hw_cursor (backend ());
          start_lock ();
          g_dbus_method_invocation_return_value (invocation, NULL);
          return;
        }
    }
  if (g_str_equal (method, "End"))
    {
      if (!owner || g_str_equal (owner, sender))
        { start_lock (); g_dbus_method_invocation_return_value (invocation, NULL); return; }
    }
  else if (g_str_equal (method, "Begin") &&
           (!meta_native_privacy_active () || (startup_guard_ready && !locking && !begin_call)))
    {
      const char *session;
      g_variant_get (params, "(&o)", &session);
      if (!validate_session (session))
        {
          g_dbus_method_invocation_return_dbus_error (invocation,
            "org.gnome.Mutter.NativeTakeover.Denied", "Invalid physical session");
          return;
        }
      gboolean from_startup_guard = startup_guard_ready;
      if (timeout_id) { g_source_remove (timeout_id); timeout_id = 0; }
      reset_lock_observation ();
      meta_native_local_lock_end ();
      startup_local_ui = startup_failed = local_authenticated = FALSE;
      startup_started = reclaim_started = 0;
      lock_reason = "remote-blanking";
      lock_retries = 0;
      g_mutex_lock (&input_mutex);
      g_atomic_int_inc (&lease_generation);
      g_atomic_int_set (&active, 1);
      g_atomic_int_set (&startup_guard, 0);
      startup_guard_ready = FALSE;
      g_mutex_unlock (&input_mutex);
      clutter_stage_set_offscreen_picking (stage (), TRUE);
      g_free (session_path);
      session_path = g_strdup (session);
      owner = g_strdup (sender);
      owner_watch = g_bus_watch_name_on_connection (connection, sender,
                        G_BUS_NAME_WATCHER_FLAGS_NONE, NULL, owner_gone, NULL, NULL);
      begin_call = g_object_ref (invocation);
      input_reset_pending = TRUE;
      {
        ClutterSeat *seat = meta_backend_get_default_seat (backend ());
        g_assert (META_IS_SEAT_NATIVE (seat));
        meta_seat_native_run_impl_task (META_SEAT_NATIVE (seat), reset_physical_input,
                                       g_object_ref (META_SEAT_NATIVE (seat)->impl), g_object_unref);
      }
      g_hash_table_remove_all (painted_views);
      g_hash_table_remove_all (presented_views);
      if (!from_startup_guard)
        {
          meta_compositor_disable_unredirect (compositor ());
          meta_backend_inhibit_hw_cursor (backend ());
        }
      clutter_actor_queue_redraw (CLUTTER_ACTOR (stage ()));
      timeout_id = g_timeout_add (5000, blank_timeout, NULL);
      return;
    }
  g_dbus_method_invocation_return_dbus_error (invocation,
    "org.gnome.Mutter.NativeTakeover.Denied", "Busy or wrong lease owner");
}

static const GDBusInterfaceVTable vtable = { .method_call = method_call };

void
meta_native_privacy_init (MetaContext *context)
{
  g_autoptr (GDBusNodeInfo) info = NULL;
  g_autoptr (GError) error = NULL;
  if (g_strcmp0 (g_getenv ("MUTTER_NATIVE_TAKEOVER"), "1") != 0) return;
  /* Shell prepends its compiled-in GI directories after reading environment
   * search paths. Reorder its existing default repository before JS starts.
   * Resolve the public GLib GI API only when loaded by Shell; plain Mutter does
   * not need or load a JavaScript/introspection dependency. */
  if (g_getenv ("MUTTER_NATIVE_TYPELIB_DIR"))
    {
      GModule *module = g_module_open (NULL, 0);
      GObject *(* dup_default) (void) = NULL;
      void (* prepend_library) (GObject *, const char *) = NULL;
      void (* prepend_search) (GObject *, const char *) = NULL;
      if (g_module_symbol (module, "gi_repository_dup_default", (gpointer *) &dup_default) &&
          g_module_symbol (module, "gi_repository_prepend_library_path", (gpointer *) &prepend_library) &&
          g_module_symbol (module, "gi_repository_prepend_search_path", (gpointer *) &prepend_search))
        {
          GObject *repository = dup_default ();
          prepend_library (repository, g_getenv ("MUTTER_NATIVE_TYPELIB_DIR"));
          prepend_search (repository, g_getenv ("MUTTER_NATIVE_TYPELIB_DIR"));
          g_object_unref (repository);
        }
      g_module_close (module);
    }
  privacy_context = context;
  test_mode = meta_backend_is_headless (backend ()) &&
              g_strcmp0 (g_getenv ("MUTTER_NATIVE_TAKEOVER_TEST"), "1") == 0;
  if (meta_backend_is_headless (backend ()) && !test_mode) return;
  if (test_mode)
    {
      const char *startup_ms = g_getenv ("MUTTER_NATIVE_TEST_STARTUP_TIMEOUT_MS");
      const char *reclaim_ms = g_getenv ("MUTTER_NATIVE_TEST_RECLAIM_TIMEOUT_MS");
      if (startup_ms) startup_timeout_us = CLAMP (g_ascii_strtoll (startup_ms, NULL, 10), 500, 30000) * 1000;
      if (reclaim_ms) reclaim_timeout_us = CLAMP (g_ascii_strtoll (reclaim_ms, NULL, 10), 500, 10000) * 1000;
    }
  system_bus = g_bus_get_sync (G_BUS_TYPE_SYSTEM, NULL, NULL);
  if (g_strcmp0 (g_getenv ("MUTTER_NATIVE_GREETER"), "1") == 0)
    {
      if (!test_mode)
        {
          greeter_session_path = find_greeter_session_with_retry ();
          if (!greeter_session_path)
            {
              g_warning ("[NativeTakeover] Refusing greeter mode: no unique native GDM seat0 session");
              return;
            }
        }
      greeter_mode = TRUE;
    }
  privacy_bus = g_bus_get_sync (G_BUS_TYPE_SESSION, NULL, &error);
  if (!privacy_bus) { g_warning ("[NativeTakeover] %s", error->message); return; }
  info = g_dbus_node_info_new_for_xml (xml, NULL);
  if (!g_dbus_connection_register_object (privacy_bus, "/org/gnome/Mutter/NativeTakeover",
                                         info->interfaces[0], &vtable, NULL, NULL, &error))
    { g_warning ("[NativeTakeover] %s", error->message); return; }
  painted_views = g_hash_table_new_full (g_direct_hash, g_direct_equal, g_object_unref, g_free);
  presented_views = g_hash_table_new (g_direct_hash, g_direct_equal);
  g_signal_connect (stage (), "presented", G_CALLBACK (presented), NULL);
  if (test_mode && greeter_mode)
    meta_remote_access_controller_inhibit_remote_access (controller ());
  if (startup_privacy_active ())
    {
      meta_remote_access_controller_inhibit_remote_access (controller ());
      remote_inhibited = TRUE;
    }
  g_atomic_int_set (&enabled, 1);
  g_bus_own_name_on_connection (privacy_bus, "org.gnome.Mutter.NativeTakeover",
                               G_BUS_NAME_OWNER_FLAGS_DO_NOT_QUEUE, NULL, NULL, NULL, NULL);
  g_message ("[NativeTakeover] Ready on existing native desktop%s", test_mode ? " (ISOLATED TEST)" : "");
}
