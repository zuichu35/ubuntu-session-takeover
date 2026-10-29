"""Compile and exercise the production libinput filter with deterministic events.

libinput getters and native-seat delivery functions are stubbed. The filter
and reset logic come from native-privacy.c. Real seat/XKB state is separately
verified by the isolated native integration test.
"""
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

PRELUDE = r'''
#include <glib.h>
#include <glib-object.h>
#include <libinput.h>
#include <linux/input-event-codes.h>
#include <stdint.h>
struct libinput_device { int id; };
struct libinput_event { enum libinput_event_type type; struct libinput_device *device; uint32_t key; int down; };
enum libinput_event_type libinput_event_get_type(struct libinput_event *e) {return e->type;}
struct libinput_device *libinput_event_get_device(struct libinput_event *e) {return e->device;}
struct libinput_event_keyboard *libinput_event_get_keyboard_event(struct libinput_event *e) {return (void*)e;}
struct libinput_event_pointer *libinput_event_get_pointer_event(struct libinput_event *e) {return (void*)e;}
struct libinput_event_tablet_tool *libinput_event_get_tablet_tool_event(struct libinput_event *e) {return (void*)e;}
uint32_t libinput_event_keyboard_get_key(struct libinput_event_keyboard *e) {return ((struct libinput_event*)e)->key;}
enum libinput_key_state libinput_event_keyboard_get_key_state(struct libinput_event_keyboard *e) {return ((struct libinput_event*)e)->down ? LIBINPUT_KEY_STATE_PRESSED : LIBINPUT_KEY_STATE_RELEASED;}
uint32_t libinput_event_pointer_get_button(struct libinput_event_pointer *e) {return ((struct libinput_event*)e)->key;}
enum libinput_button_state libinput_event_pointer_get_button_state(struct libinput_event_pointer *e) {return ((struct libinput_event*)e)->down ? LIBINPUT_BUTTON_STATE_PRESSED : LIBINPUT_BUTTON_STATE_RELEASED;}
uint32_t libinput_event_tablet_tool_get_button(struct libinput_event_tablet_tool *e) {return ((struct libinput_event*)e)->key;}
enum libinput_button_state libinput_event_tablet_tool_get_button_state(struct libinput_event_tablet_tool *e) {return ((struct libinput_event*)e)->down ? LIBINPUT_BUTTON_STATE_PRESSED : LIBINPUT_BUTTON_STATE_RELEASED;}
enum libinput_tablet_tool_tip_state libinput_event_tablet_tool_get_tip_state(struct libinput_event_tablet_tool *e) {return ((struct libinput_event*)e)->down ? LIBINPUT_TABLET_TOOL_TIP_DOWN : LIBINPUT_TABLET_TOOL_TIP_UP;}
struct libinput_event_touch *libinput_event_get_touch_event(struct libinput_event *e) {return (void*)e;}
int32_t libinput_event_touch_get_seat_slot(struct libinput_event_touch *e) {return ((struct libinput_event*)e)->key;}
struct libinput_event_gesture *libinput_event_get_gesture_event(struct libinput_event *e) {return (void*)e;}
int libinput_event_gesture_get_finger_count(struct libinput_event_gesture *e) {return 3;}
void *libinput_device_get_user_data(struct libinput_device *d) {return d;}
typedef struct libinput_device ClutterInputDevice;
typedef struct {int button_count[KEY_CNT];} MetaSeatImpl;
typedef struct {float x,y;} graphene_point_t;
typedef int ClutterEvent;
#define CLUTTER_EVENT_NONE 0
#define CLUTTER_TOUCH_CANCEL 1
#define CLUTTER_TOUCHPAD_GESTURE_PHASE_CANCEL 1
static int key_releases, button_releases, touch_cancels, gesture_cancels;
static gboolean input_reset_finished(gpointer p) {return G_SOURCE_REMOVE;}
static void meta_seat_impl_notify_key_in_impl(MetaSeatImpl *s, ClutterInputDevice *d, uint64_t t, uint32_t k, uint32_t v, gboolean update) {g_assert_cmpint(v,==,0); g_assert_cmpint(s->button_count[k],>,0); s->button_count[k]--; key_releases++;}
static void meta_seat_impl_notify_button_in_impl(MetaSeatImpl *s, ClutterInputDevice *d, uint64_t t, uint32_t k, uint32_t v) {g_assert_cmpint(v,==,0); g_assert_cmpint(s->button_count[k],>,0); s->button_count[k]--; button_releases++;}
static void meta_seat_impl_notify_touch_event_in_impl(MetaSeatImpl *s, ClutterInputDevice *d, int type, uint64_t t, int slot, float x, float y) {g_assert_cmpint(type,==,CLUTTER_TOUCH_CANCEL); touch_cancels++;}
static ClutterEvent *clutter_event_touchpad_pinch_new(int f, uint64_t t, ClutterInputDevice *d, int phase, guint fingers, graphene_point_t p, graphene_point_t delta, graphene_point_t raw, float angle, float scale) {g_assert_cmpint(phase,==,1);return g_new0(int,1);}
static ClutterEvent *clutter_event_touchpad_swipe_new(int f, uint64_t t, ClutterInputDevice *d, int phase, guint fingers, graphene_point_t p, graphene_point_t delta, graphene_point_t raw) {g_assert_cmpint(phase,==,1);return g_new0(int,1);}
static ClutterEvent *clutter_event_touchpad_hold_new(int f, uint64_t t, ClutterInputDevice *d, int phase, guint fingers, graphene_point_t p) {g_assert_cmpint(phase,==,1);return g_new0(int,1);}
static void _clutter_event_push(ClutterEvent *e, gboolean copy) {gesture_cancels++;g_free(e);}
static gint enabled = 1, active, auth_events;
static gboolean meta_native_privacy_active(void) {return g_atomic_int_get(&active);}
static void queue_local_key(guint key, gboolean down) {auth_events++;}
'''

MAIN = r'''
static struct libinput_device a = {1}, b = {2}, receiver = {3};
static MetaSeatImpl seat;
static gboolean event(struct libinput_device *d, enum libinput_event_type type, guint key, int down) {
  struct libinput_event e = {type,d,key,down};
  gboolean drop = meta_native_privacy_filter_input(&e);
  if (!drop && (type == LIBINPUT_EVENT_KEYBOARD_KEY || type == LIBINPUT_EVENT_POINTER_BUTTON)) {
    guint count = meta_native_privacy_key_count(key, 99);
    if ((down && count == 1) || (!down && count == 0)) seat.button_count[key] += down ? 1 : -1;
  }
  if (!drop && type == LIBINPUT_EVENT_TABLET_TOOL_TIP) seat.button_count[BTN_TOUCH] += down ? 1 : -1;
  return drop;
}
#define SEND(d,t,k,v) event(&(d),LIBINPUT_EVENT_##t,k,v)
int main(void) {
  g_assert_false(input_reset_pending);
  /* Any device's stuck keys, modifiers and drag must be neutralized. */
  SEND(receiver,KEYBOARD_KEY,KEY_BRIGHTNESSDOWN,1);
  SEND(receiver,KEYBOARD_KEY,KEY_BRIGHTNESSUP,1);
  SEND(receiver,KEYBOARD_KEY,KEY_MICMUTE,1);
  SEND(a,KEYBOARD_KEY,KEY_LEFTCTRL,1);
  SEND(b,KEYBOARD_KEY,KEY_LEFTCTRL,1);
  SEND(a,KEYBOARD_KEY,KEY_A,1);
  SEND(a,POINTER_BUTTON,BTN_LEFT,1);
  g_assert_cmpuint(physical_down,==,7);
  g_assert_cmpint(seat.button_count[KEY_LEFTCTRL],==,1);
  active=1;
  reset_physical_input_in_impl(&seat);
  g_assert_cmpint(key_releases,==,5);
  g_assert_cmpint(button_releases,==,1);
  for (guint k=0;k<KEY_CNT;k++) g_assert_cmpint(seat.button_count[k],==,0);
  g_assert_cmpuint(physical_down,==,7); /* No falsifying hardware state. */
  g_assert_true(SEND(a,POINTER_MOTION,0,0));
  g_assert_true(SEND(a,KEYBOARD_KEY,KEY_Z,1));
  /* Returning local never globally waits for those stuck keys. */
  active=0;
  g_assert_false(SEND(a,POINTER_MOTION,0,0));
  g_assert_false(SEND(a,KEYBOARD_KEY,KEY_B,1));
  g_assert_false(SEND(a,KEYBOARD_KEY,KEY_B,0));
  g_assert_false(SEND(a,POINTER_BUTTON,BTN_RIGHT,1));
  g_assert_false(SEND(a,POINTER_BUTTON,BTN_RIGHT,0));
  g_assert_true(SEND(a,KEYBOARD_KEY,KEY_Z,0));
  g_assert_true(SEND(a,POINTER_BUTTON,BTN_LEFT,0));
  g_assert_false(SEND(a,POINTER_BUTTON,BTN_LEFT,1));
  g_assert_false(SEND(a,POINTER_BUTTON,BTN_LEFT,0));
  /* Another keyboard holding Ctrl must not swallow a fresh Ctrl cycle. */
  g_assert_true(SEND(a,KEYBOARD_KEY,KEY_LEFTCTRL,0));
  g_assert_false(SEND(a,KEYBOARD_KEY,KEY_LEFTCTRL,1));
  g_assert_cmpint(seat.button_count[KEY_LEFTCTRL],==,1);
  g_assert_false(SEND(a,KEYBOARD_KEY,KEY_LEFTCTRL,0));
  g_assert_cmpint(seat.button_count[KEY_LEFTCTRL],==,0);
  /* Reconnect succeeds even though the receiver and second Ctrl stay down. */
  active=1; reset_physical_input_in_impl(&seat);
  g_assert_cmpint(key_releases,==,5);
  active=0;
  SEND(a,DEVICE_REMOVED,0,0); SEND(b,DEVICE_REMOVED,0,0); SEND(receiver,DEVICE_REMOVED,0,0);
  g_assert_cmpuint(physical_down,==,0);
  /* Touch/gesture cancellation and tip release preserve a usable keyboard. */
  SEND(a,TOUCH_DOWN,2,0); SEND(a,TABLET_TOOL_TIP,0,1); SEND(a,GESTURE_PINCH_BEGIN,0,0);
  active=1; reset_physical_input_in_impl(&seat);
  g_assert_cmpint(touch_cancels,==,1);
  g_assert_cmpint(gesture_cancels,==,1);
  g_assert_cmpint(seat.button_count[BTN_TOUCH],==,0);
  active=0;
  g_assert_false(SEND(b,KEYBOARD_KEY,KEY_B,1));
  SEND(b,KEYBOARD_KEY,KEY_B,0);
  g_assert_true(SEND(a,TOUCH_UP,2,0));
  g_assert_true(SEND(a,TABLET_TOOL_TIP,0,0));
  g_assert_true(SEND(a,GESTURE_PINCH_END,0,0));
  g_assert_false(SEND(a,TOUCH_DOWN,3,0)); SEND(a,TOUCH_UP,3,0);
  /* Local keys go only to the independent authentication input path.
   * Ctrl+Alt+Del cannot disconnect a remote owner without authentication. */
  active=1;
  int before_auth_events = auth_events;
  SEND(a,KEYBOARD_KEY,KEY_LEFTCTRL,1); SEND(a,KEYBOARD_KEY,KEY_LEFTALT,1); SEND(a,KEYBOARD_KEY,KEY_DELETE,1);
  while(g_main_context_pending(NULL)) g_main_context_iteration(NULL,FALSE);
  g_assert_cmpint(auth_events,>,before_auth_events);
  SEND(a,DEVICE_REMOVED,0,0); SEND(b,DEVICE_REMOVED,0,0);
  g_assert_cmpuint(physical_down,==,0);
  enabled=0;
  g_assert_false(SEND(a,KEYBOARD_KEY,KEY_B,1));
  g_assert_cmpuint(meta_native_privacy_key_count(KEY_B,42),==,42);
  g_hash_table_unref(physical_devices);
  return 0;
}
'''


class NativeInput(unittest.TestCase):
    def test_raw_input_filter(self):
        source = (ROOT / 'takeover/native-privacy.c').read_text()
        state = source.split('static GMutex input_mutex;', 1)[1].split('static const char xml[]', 1)[0]
        function = 'static void\nphysical_device_free' + source.split('static void\nphysical_device_free', 1)[1].split('/* Releases are deliberately', 1)[0]
        reset = 'static gboolean\nreset_physical_input_in_impl' + source.split('static gboolean\nreset_physical_input_in_impl', 1)[1].split('static gboolean\nreset_physical_input (', 1)[0]
        code = PRELUDE + 'static GMutex input_mutex;' + state + function + reset + MAIN
        flags = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'glib-2.0', 'gobject-2.0'], text=True))
        with tempfile.TemporaryDirectory(prefix='native-input-') as d:
            path = Path(d)
            (path / 'test.c').write_text(code)
            subprocess.run(['cc', '-Wall', '-Werror', '-g', '-fsanitize=address,undefined',
                            str(path / 'test.c'), '-o', str(path / 'test'), *flags], check=True, capture_output=False)
            subprocess.run([str(path / 'test')], check=True, capture_output=False)


if __name__ == '__main__':
    unittest.main()
