#!/usr/bin/python3
"""Run the real private Mutter against two independent fault-injected locks."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from gi.repository import Gio, GLib

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path(os.environ['NATIVE_TEST_RUNTIME_ROOT'])
RESULTS = Path(os.environ['NATIVE_TEST_ARTIFACT_ROOT']) / 'startup-lock'
NAME = 'org.gnome.Mutter.NativeTakeover'
OBJECT = '/org/gnome/Mutter/NativeTakeover'


class Desktop:
    def __init__(self, name, config, timeout_ms=4000, reclaim_ms=1000):
        self.directory = tempfile.TemporaryDirectory(prefix='native-startup-')
        self.run = Path(self.directory.name)
        self.art = RESULTS / name
        self.art.mkdir(parents=True, exist_ok=True)
        self.children, self.logs = [], []
        self.config = self.run / 'lock.json'
        self.write_config(config)
        self.policy = self.run / 'auth-policy'
        self.policy.write_text('accept')
        helper = self.run / 'auth'
        helper.write_text('#!/usr/bin/python3\nimport sys,time\nfrom pathlib import Path\n'
                          'p=sys.stdin.buffer.read()\n'
                          'v=Path(' + repr(str(self.policy)) + ').read_text()\n'
                          'if v=="delayed": time.sleep(2)\n'
                          'sys.exit(0 if v in ("accept","delayed") and p==b"a" else 1)\n')
        helper.chmod(0o700)
        env = dict(os.environ, XDG_RUNTIME_DIR=str(self.run), XDG_CONFIG_HOME=str(self.run / 'config'),
                   XDG_DATA_HOME=str(self.run / 'data'), XDG_CACHE_HOME=str(self.run / 'cache'),
                   GSETTINGS_BACKEND='memory', LIBGL_ALWAYS_SOFTWARE='1', GALLIUM_DRIVER='llvmpipe')
        for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'DCONF_PROFILE',
                    'GSETTINGS_SCHEMA_DIR', 'NOTIFY_SOCKET', 'WATCHDOG_USEC', 'WATCHDOG_PID'):
            env.pop(key, None)
        bus = subprocess.Popen(['dbus-daemon', '--session', '--nofork', '--print-address=1'],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.children.append(bus)
        env['DBUS_SESSION_BUS_ADDRESS'] = bus.stdout.readline().strip()
        self.bus = Gio.DBusConnection.new_for_address_sync(env['DBUS_SESSION_BUS_ADDRESS'],
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)
        self.spawn(['/usr/bin/python3', ROOT / 'tests/native_screen_saver.py', self.config], env, 'locks')
        prefix = RUNTIME / 'build/native-prefix'
        lib = prefix / 'lib/x86_64-linux-gnu'
        native = dict(env, LD_LIBRARY_PATH=f'{lib}:{lib}/mutter-18', MUTTER_NATIVE_TAKEOVER='1',
                      MUTTER_NATIVE_TAKEOVER_TEST='1', MUTTER_NATIVE_BOOT_PRIVACY='1',
                      MUTTER_NATIVE_TEST_AUTH_HELPER=str(helper),
                      MUTTER_NATIVE_TEST_FRAME=str(self.art / 'physical.png'),
                      MUTTER_NATIVE_TEST_STARTUP_TIMEOUT_MS=str(timeout_ms),
                      MUTTER_NATIVE_TEST_RECLAIM_TIMEOUT_MS=str(reclaim_ms))
        self.compositor = self.spawn([prefix / 'bin/mutter', '--headless', '--no-x11', '--wayland',
            '--wayland-display=startup-test', '--virtual-monitor=1280x720',
            '--mutter-plugin=' + str(lib / 'mutter-18/plugins/libdefault.so')], native, 'mutter')
        self.wait(lambda: self.status()['state'] == 'boot-guard', 10)
        app_code = ("import gi;gi.require_version('Gtk','4.0');from gi.repository import Gtk;"
                    "a=Gtk.Application(application_id='test.private.Startup');"
                    "a.connect('activate',lambda a:(lambda w:(w.set_default_size(900,600),"
                    "w.set_child(Gtk.Label(label='PRIVATE DESKTOP')),w.present()))(Gtk.ApplicationWindow(application=a)));a.run([])")
        self.app = self.spawn(['/usr/bin/python3', '-c', app_code],
                              dict(env, WAYLAND_DISPLAY='startup-test', GDK_BACKEND='wayland'), 'app')
        self.original_pid = self.compositor.pid
        self.samples = []
        self.call('PrepareLogin', GLib.Variant('(o)', ('/test',)))

    def spawn(self, args, env, name):
        handle = (self.art / (name + '.log')).open('w')
        self.logs.append(handle)
        child = subprocess.Popen([str(v) for v in args], env=env, stdout=handle, stderr=handle)
        self.children.append(child)
        return child

    def write_config(self, value):
        temp = self.config.with_suffix('.tmp')
        temp.write_text(json.dumps(value))
        temp.replace(self.config)

    def call(self, method, params=None):
        return self.bus.call_sync(NAME, OBJECT, NAME, method, params, None, 0, 2500, None).unpack()

    def status(self):
        started = time.monotonic()
        status = self.call('GetStatus')[0]
        assert time.monotonic() - started < 1.0, 'Compositor D-Bus blocked on lock confirmation'
        return status

    def wait(self, predicate, seconds=6):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            assert self.compositor.poll() is None, 'Private compositor exited'
            try:
                if predicate():
                    return
            except GLib.Error:
                pass
            time.sleep(.05)
        raise AssertionError('Isolated startup-lock condition timed out')

    def key(self, key):
        for down in (True, False):
            self.call('TestLocalKey', GLib.Variant('(ub)', (key, down)))

    def assert_failed_surface(self):
        assert self.status()['state'] == 'startup-failed'
        self.call('InspectPhysical')
        from PIL import Image
        with Image.open(self.art / 'physical.png') as frame:
            cx, cy = frame.width // 2, frame.height // 2
            assert min(frame.getpixel((cx - 190, cy + 40))[:3]) > 30, 'Recovery password field not visible'
            assert max(frame.getpixel((5, 5))[:3]) == 0, 'Private desktop leaked outside recovery surface'
        try:
            self.call('Begin', GLib.Variant('(o)', ('/test',)))
        except GLib.Error:
            pass
        else:
            raise AssertionError('Unconfirmed startup lock granted remote access')

    def close(self):
        for child in reversed(self.children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        for handle in self.logs:
            handle.close()
        self.directory.cleanup()


def run_case(name, config, check, **kwargs):
    desktop = None
    try:
        desktop = Desktop(name, config, **kwargs)
        result = check(desktop)
        assert desktop.compositor.pid == desktop.original_pid and desktop.app.poll() is None
        (desktop.art / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        return result
    finally:
        if desktop:
            desktop.close()


def main():
    assert os.readlink('/proc/self/ns/net') != os.environ['NATIVE_TEST_HOST_NET_NS']
    assert not list(Path('/dev/dri').glob('*')), 'Test requires hidden physical DRM devices'
    results = {}
    def delayed_hint(d):
        d.wait(lambda: d.status()['screensaver-active'] and not d.status()['logind-locked'])
        assert d.status()['state'] == 'startup-locking'
        d.wait(lambda: d.status()['state'] == 'ready-guarded')
        status = d.status()
        assert status['screensaver-active'] and status['logind-locked'] and status['lock-retries'] >= 2
        return {'delayed_logind_hint_repaired_by_real_lock_retry': True, 'status': status}
    results['delayed_hint'] = run_case('delayed-hint', {'hint_after_lock': 2}, delayed_hint)

    def delayed_active(d):
        d.wait(lambda: d.status()['logind-locked'] and not d.status()['screensaver-active'])
        assert d.status()['state'] == 'startup-locking'
        d.write_config({})
        d.wait(lambda: d.status()['state'] == 'ready-guarded')
        return {'active_false_hint_true_remains_private_then_recovers': True}
    results['delayed_active'] = run_case('delayed-active', {'fail_lock': True}, delayed_active)

    def failed_reclaim(d):
        d.wait(lambda: d.status()['state'] == 'startup-failed')
        d.assert_failed_surface()
        d.key(48)  # b: deliberately wrong password
        d.key(28)
        time.sleep(.5)
        assert not d.status()['local-authenticated']
        d.assert_failed_surface()
        time.sleep(2.1)
        d.key(30)  # a, kept in the private input buffer during background retries
        before = d.status()['lock-retries']
        d.wait(lambda: d.status()['lock-retries'] > before, 7)
        d.key(28)
        d.wait(lambda: d.status()['local-authenticated'])
        d.wait(lambda: d.status()['state'] == 'local', 4)
        status = d.status()
        assert status['remote-inhibited'] and not status['has-owner'] and status['session-path'] == '/test'
        assert status['reason'] == 'local-authenticated-shell-timeout'
        return {'wrong_password_preserves_privacy': True, 'input_survives_background_retry': True,
                'authenticated_local_recovery_despite_unavailable_shell': True, 'status': status}
    results['failed_reclaim'] = run_case('failed-reclaim', {
        'fail_active_read': True, 'fail_hint_read': True, 'fail_unlock': True, 'fail_hint_unlock': True},
        failed_reclaim, timeout_ms=600)

    def late_callback(d):
        d.wait(lambda: d.status()['state'] == 'startup-failed')
        d.key(30)
        d.key(28)
        d.wait(lambda: d.status()['state'] == 'local', 3)
        time.sleep(3.2)
        status = d.status()
        assert status['state'] == 'local' and status['local-authenticated'] and status['remote-inhibited']
        return {'late_lock_callbacks_cannot_reactivate_native_privacy_after_local_auth': True, 'status': status}
    results['late_callback'] = run_case('late-callback', {
        'hint_delay_ms': 3000, 'fail_unlock': True, 'fail_hint_unlock': True},
        late_callback, timeout_ms=600, reclaim_ms=600)
    results['tested_library_sha256'] = hashlib.sha256((RUNTIME / 'build/native-prefix/lib/x86_64-linux-gnu/libmutter-18.so.0').read_bytes()).hexdigest()
    results['real_pam_tested'] = False
    results['physical_session_modified'] = False
    (RESULTS / 'result.json').write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
