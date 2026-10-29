#!/usr/bin/env python3
"""Shared private-process helpers used by the native integration tests.

The standalone main below is the historical 3391 viewer experiment, whose
viewer build/install entrypoints are not distributed in this native release.
Use native_integration.py for the supported 3392 integration scenarios.
"""
import json
import os
from pathlib import Path
import signal
import secrets
import sys
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / 'artifacts'
RUNTIME = ART / 'runtime'
children = []
handles = []


def spawn(args, env, name, **kwargs):
    log = open(ART / f'{name}.log', 'w')
    handles.append(log)
    p = subprocess.Popen(args, env=env, stdout=log, stderr=log, **kwargs)
    children.append(p)
    return p


def wait_for(test, message, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if test():
            return
        time.sleep(.1)
    raise AssertionError(message)


def wait_for_client_window(process, environment, title, seconds=15, *,
                           run=subprocess.run, sleep=time.sleep, clock=time.monotonic):
    """Authentication precedes X11 window mapping; wait for the actual client."""
    import re
    deadline = clock() + seconds
    while clock() < deadline:
        if process.poll() is not None:
            raise AssertionError(f'RDP client {title} exited with status {process.returncode} before its window was ready')
        result = run(['xdotool', 'search', '--onlyvisible', '--name', '^' + re.escape(title) + '$'],
                     env=environment, capture_output=True, text=True, timeout=2)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.splitlines()[-1]
        sleep(.1)
    windows = []
    try:
        visible = run(['xdotool', 'search', '--onlyvisible', '--name', '.'],
                      env=environment, capture_output=True, text=True, timeout=2)
        for identifier in visible.stdout.splitlines()[:20]:
            name = run(['xdotool', 'getwindowname', identifier], env=environment,
                       capture_output=True, text=True, timeout=1)
            windows.append({'id': identifier, 'title': name.stdout.strip()[:200]})
    except (OSError, subprocess.TimeoutExpired):
        windows.append({'diagnostic': 'window enumeration unavailable'})
    raise AssertionError(f'RDP client {title} did not map its window within {seconds}s; '
                         f'client exit={process.poll()}; visible Xvfb windows={windows}')


def stop(p):
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def main():
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(RUNTIME, 0o700)
    env = dict(os.environ, XDG_RUNTIME_DIR=str(RUNTIME),
               XDG_CONFIG_HOME=str(RUNTIME / 'config'),
               XDG_CACHE_HOME=str(RUNTIME / 'cache'),
               XDG_DATA_HOME=str(RUNTIME / 'data'), GSETTINGS_BACKEND='memory',
               LIBGL_ALWAYS_SOFTWARE='1', GSK_RENDERER='cairo')
    for key in ['DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER', 'PIPEWIRE_REMOTE']:
        env.pop(key, None)
    bus = subprocess.Popen(['dbus-daemon', '--session', '--nofork', '--print-address=1'],
                           env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    children.append(bus)
    env['DBUS_SESSION_BUS_ADDRESS'] = bus.stdout.readline().strip()
    (ART / 'test-bus.txt').write_text(env['DBUS_SESSION_BUS_ADDRESS'])
    pw = spawn(['pipewire'], env, 'pipewire')
    wait_for(lambda: (RUNTIME / 'pipewire-0').exists(), 'private PipeWire missing')
    wpconf = RUNTIME / 'config/wireplumber/wireplumber.conf.d'
    wpconf.mkdir(parents=True, exist_ok=True)
    (wpconf / '99-test-no-hardware.conf').write_text('wireplumber.profiles = { main = { monitor.alsa = disabled monitor.alsa-midi = disabled monitor.bluez = disabled monitor.bluez-midi = disabled monitor.v4l2 = disabled monitor.libcamera = disabled } }')
    spawn(['wireplumber'], env, 'wireplumber')
    pkg = ROOT / 'vendor/ubuntu-mutter/root/usr'
    compositor_args = ['/usr/bin/gnome-shell'] if '--shell' in sys.argv else [str(pkg / 'bin/mutter')]
    compositor_args += ['--headless', '--no-x11', '--wayland',
                        '--wayland-display=takeover-test', '--virtual-monitor=1280x720']
    if '--shell' not in sys.argv:
        compositor_args += ['--mutter-plugin=' + str(pkg / 'lib/x86_64-linux-gnu/mutter-18/plugins/libdefault.so')]
    compositor = spawn(compositor_args, env, 'mutter')
    wait_for(lambda: (RUNTIME / 'takeover-test').exists(), 'headless test compositor failed')
    from gi.repository import Gio
    conn = Gio.DBusConnection.new_for_address_sync(env['DBUS_SESSION_BUS_ADDRESS'],
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)

    def monitors():
        return conn.call_sync('org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig',
                              'org.gnome.Mutter.DisplayConfig', 'GetCurrentState', None, None, 0, 5000, None).unpack()

    def ready():
        try:
            return bool(monitors()[1])
        except Exception:
            return False
    wait_for(ready, 'compositor D-Bus services missing')
    before = monitors()
    connector = before[1][0][0][0]
    app_state = ART / 'app-state.json'
    app_state.unlink(missing_ok=True)
    target_env = dict(env, WAYLAND_DISPLAY='takeover-test', GDK_BACKEND='wayland',
                      TAKEOVER_TEST_APP_STATE=str(app_state))
    app = spawn(['/usr/bin/python3', str(ROOT / 'tests/desktop_app.py')], target_env, 'desktop-app')
    wait_for(app_state.exists, 'test app failed')
    read_fd, write_fd = os.pipe()
    xvfb = spawn(['Xvfb', '-displayfd', str(write_fd), '-screen', '0', '1500x1000x24', '-nolisten', 'tcp'],
                 env, 'xvfb', pass_fds=(write_fd,))
    os.close(write_fd)
    with os.fdopen(read_fd) as pipe:
        display = ':' + pipe.readline().strip()
    viewer_env = dict(env, DISPLAY=display, GDK_BACKEND='x11',
                      GSETTINGS_SCHEMA_DIR=str(ROOT / 'build'), G_MESSAGES_DEBUG='all',
                      LD_LIBRARY_PATH=str(ROOT / 'vendor/ubuntu-freerdp/root/usr/lib/x86_64-linux-gnu'))

    def viewer(name):
        p = spawn([str(ROOT / 'build/session-viewer'), '--attach-monitor', connector,
                   '--target-bus', env['DBUS_SESSION_BUS_ADDRESS']], viewer_env, name)
        wait_for(lambda: 'Pipewire stream state changed from paused to streaming' in (ART / f'{name}.log').read_text(),
                 'viewer did not receive PipeWire video', 25)
        time.sleep(1)
        return p

    if '--shell' in sys.argv:
        wait_for(lambda: 'GNOME Shell started' in (ART / 'mutter.log').read_text(), 'GNOME Shell did not finish startup')
    first = viewer('viewer-first')
    current = monitors()
    assert before[1] == current[1] and before[2] == current[2], 'viewer changed target monitor topology'
    # XTest acts only on the private Xvfb viewer. The application receives libei input.
    def xd(*args):
        return subprocess.check_output(['xdotool', *args], env=viewer_env, text=True).strip()
    windows = xd('search', '--onlyvisible', '--class', 'session-viewer').splitlines()
    assert windows, 'no viewer window'
    wid = windows[-1]
    xd('windowfocus', wid)
    xd('mousemove', '--window', wid, '450', '345')
    xd('click', '1')
    time.sleep(.5)  # Allow GNOME focus animation to complete before typing.
    xd('key', 'ctrl+a')
    xd('type', '--clearmodifiers', '--delay', '80', 'same-session-123')
    xd('mousemove', '--window', wid, '450', '420')
    xd('click', '1')
    # Position/focus is diagnosed using a screenshot if input routing differs.
    time.sleep(1)
    from PIL import ImageGrab
    ImageGrab.grab(xdisplay=display).save(ART / 'viewer-first.png')
    first_state = json.loads(app_state.read_text())
    stop(first)
    wait_for(lambda: len(monitors()[1]) == len(before[1]), 'viewer teardown changed target topology')
    assert compositor.poll() is None and app.poll() is None, 'viewer exit killed desktop/application'
    second = viewer('viewer-second')
    second_state = json.loads(app_state.read_text())
    assert second_state == first_state, 'application state changed across reconnect'
    assert monitors()[1] == before[1], 'reconnect created a monitor'
    rdp_verified = False
    rdp_state = None
    if '--rdp' in sys.argv:
        # Network namespace is mandatory: no test listener is exposed to the LAN.
        assert os.readlink('/proc/self/ns/net') != os.readlink('/proc/1/ns/net'), 'RDP test requires a private network namespace'
        stop(second)
        cert, key = RUNTIME / 'rdp.crt', RUNTIME / 'rdp.key'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=takeover-isolated-test', '-keyout', str(key), '-out', str(cert)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        password = secrets.token_urlsafe(24)
        grd_env = dict(env, GSETTINGS_BACKEND='keyfile', GNOME_REMOTE_DESKTOP_TEST_RDP_USERNAME='takeover-test',
                       GNOME_REMOTE_DESKTOP_TEST_RDP_PASSWORD=password)
        for k, v in [('tls-cert', str(cert)), ('tls-key', str(key)), ('view-only', 'false'), ('enable', 'true')]:
            subprocess.run(['gsettings', 'set', 'org.gnome.desktop.remote-desktop.rdp', k, v],
                           env=grd_env, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['gsettings', 'set', 'org.gnome.desktop.remote-desktop.rdp', 'screen-share-mode', 'mirror-primary'], env=grd_env, check=True)
        grd = spawn(['/usr/libexec/gnome-remote-desktop-daemon', '--rdp-port=43389'], grd_env, 'grd')
        wait_for(lambda: 'RDP server started' in (ART / 'grd.log').read_text(), 'isolated RDP server failed', 20)
        rdp = spawn([str(ROOT / 'vendor/ubuntu-freerdp/root/usr/bin/xfreerdp'), '/v:127.0.0.1:43389',
                     '/u:takeover-test', '/p:' + password, '/cert:ignore', '/size:1280x720', '/gfx:RFX',
                     '/network:lan', '/title:TakeoverRDPTest'], viewer_env, 'rdp-client')
        wait_for(lambda: b'TakeoverRDPTest' in subprocess.run(['xdotool', 'search', '--name', 'TakeoverRDPTest', 'getwindowname'],
                    env=viewer_env, capture_output=True).stdout, 'RDP window not ready', 25)
        time.sleep(3)
        wid = xd('search', '--onlyvisible', '--name', 'TakeoverRDPTest').splitlines()[-1]
        xd('windowfocus', wid)
        xd('mousemove', '--window', wid, '450', '295')
        xd('click', '1')
        xd('key', 'ctrl+a')
        xd('type', '--clearmodifiers', '--delay', '80', 'rdp-same-session-456')
        time.sleep(1)
        ImageGrab.grab(xdisplay=display).save(ART / 'rdp.png')
        rdp_state = json.loads(app_state.read_text())
        rdp_verified = rdp_state['text'] == 'rdp-same-session-456' and rdp_state['pid'] == app.pid
        stop(rdp)
        stop(grd)
        third = viewer('viewer-after-rdp')
        assert json.loads(app_state.read_text()) == rdp_state, 'RDP disconnect lost application state'
        assert monitors()[1] == before[1], 'RDP changed persistent monitor'
        assert rdp_verified, 'RDP input failed; inspect rdp.png'
        if '--gate' in sys.argv:
            stop(third)
            from gate_test import run as run_gate_tests
            run_gate_tests(ROOT, RUNTIME, env, viewer_env, connector, app.pid, app_state,
                           spawn, stop, wait_for)
    result = {'compositor_pid': compositor.pid, 'application_pid': app.pid, 'connector': connector,
              'pipewire_streaming': True, 'monitor_topology_preserved': True,
              'viewer_exit_preserves_desktop': True, 'reconnect_preserves_app_state': True,
              'input_state': first_state,
              'rdp_input_state': rdp_state,
              'keyboard_verified': first_state['text'] == 'same-session-123',
              'mouse_verified': first_state['clicks'] == 1,
              'linux_rdp_same_session_verified': rdp_verified,
              'windows_rdp_tested': False, 'physical_privacy_tested': False}
    (ART / 'integration-result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2), flush=True)
    assert result['mouse_verified'], 'mouse click did not reach target'
    assert result['keyboard_verified'], 'keyboard did not reach target entry; inspect isolated screenshot'


if __name__ == '__main__':
    try:
        main()
    finally:
        for child in reversed(children):
            stop(child)
        for handle in handles:
            handle.close()
        for name in ['rdp.key', 'rdp.crt']:
            (RUNTIME / name).unlink(missing_ok=True)
        (ART / 'test-bus.txt').unlink(missing_ok=True)
