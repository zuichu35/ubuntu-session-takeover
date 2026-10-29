#!/usr/bin/env python3
"""Run GRD against the current physical desktop, never start another desktop."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from gi.repository import Gio, GLib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from takeover.session_health import backend_listening, GatewayRegistration, Watchdog
from takeover.greeter_config import grd_environment as greeter_grd_environment
from takeover.login_gateway import boot_guard_present
from takeover.startup_guard import (READY_STATES, STARTUP_STATES, LocalReclaimRejected,
                                    finish_local_reclaim, wait_startup_ready)

CONFIG = Path.home() / '.config/ubuntu-native-takeover'
DATA = Path.home() / '.local/share/ubuntu-native-takeover'
RUN = Path(f'/run/user/{os.getuid()}/ubuntu-native-takeover')


def report_child_exit(broker, grd):
    stopped = [(name, process.poll()) for name, process in (('broker', broker), ('RDP daemon', grd))]
    failures = [f'{name} exited with status {code}' for name, code in stopped if code is not None]
    if failures:
        raise RuntimeError('Native takeover stopped: ' + '; '.join(failures))


def recover_monitor_layout_before_broker(state):
    if state == 'local':
        subprocess.run(['/usr/bin/python3', '-m', 'takeover.monitor_layout'], cwd=ROOT, check=True)
    elif state != 'ready-guarded':
        raise RuntimeError('Monitor recovery requires a ready native desktop')
    # The standalone recovery tool waits for local privacy state. During
    # ready-guarded startup, the broker's existing restore path safely restores
    # a journal while the physical privacy barrier stays active.


def retire_legacy_services(enabled=False):
    """Retire old entrypoints only when the operator explicitly opts in."""
    if enabled:
        subprocess.run(['systemctl', '--user', 'stop', 'ubuntu-takeover-broker.service',
                        'ubuntu-takeover-rdp.service'], check=False)


def main():
    os.umask(0o077)
    watchdog = Watchdog()
    watchdog.ping()
    greeter = '--greeter' in sys.argv
    gateway_enabled = Path('/etc/ubuntu-native-login/config.json').exists()
    if greeter and not gateway_enabled:
        raise RuntimeError('GDM remote entry requires the system login gateway')
    # Fail before spawning children if root-managed greeter credentials or
    # settings are absent. Never fall back to a dynamic account's empty HOME.
    greeter_env = greeter_grd_environment({}) if greeter else None
    port = (3394 if greeter else 3393) if gateway_enabled else 3392
    registration = None
    RUN.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (RUN / 'supervisor.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    user_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    # Diagnose the actual runtime interface, not the installed package version.
    try:
        state = user_bus.call_sync('org.gnome.Mutter.NativeTakeover', '/org/gnome/Mutter/NativeTakeover',
            'org.gnome.Mutter.NativeTakeover', 'GetState', None, None, 0, 3000, None).unpack()[0]
    except GLib.Error as error:
        raise SystemExit('Native Mutter patch is not loaded; port 3392 cannot start. '
                         'Check journalctl --user -u org.gnome.Shell@ubuntu.service '
                         'for loader/runtime errors. If the launcher was just installed, '
                         f'log in again to load it. D-Bus error: {error}')
    if state not in READY_STATES | STARTUP_STATES:
        raise RuntimeError('Native privacy state is not local; refusing a duplicate controller')
    sessions = json.loads(subprocess.check_output(['loginctl', 'list-sessions', '--json=short']))
    candidates = []
    for s in sessions:
        if s.get('uid') != os.getuid() or s.get('seat') != 'seat0' or s.get('class') != ('greeter' if greeter else 'user'):
            continue
        p = dict(l.split('=', 1) for l in subprocess.check_output(['loginctl', 'show-session', str(s['session']),
                    '-p', 'Type', '-p', 'Active', '-p', 'Remote'], text=True).splitlines())
        if p == {'Type': 'wayland', 'Active': 'yes', 'Remote': 'no'}:
            candidates.append(str(s['session']))
    if len(candidates) != 1:
        raise RuntimeError('Exactly one active physical Wayland session is required')
    session = system_bus.call_sync('org.freedesktop.login1', '/org/freedesktop/login1',
        'org.freedesktop.login1.Manager', 'GetSession', GLib.Variant('(s)', (candidates[0],)),
        None, 0, 2000, None).unpack()[0]
    def native_call(method, parameters=None):
        return user_bus.call_sync('org.gnome.Mutter.NativeTakeover', '/org/gnome/Mutter/NativeTakeover',
            'org.gnome.Mutter.NativeTakeover', method, parameters,
            None, 0, 3000, None).unpack()

    def shutdown(*args):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    # Shell has already completed its start job. Only explicitly marked startup
    # transitions may wait here; a different controller's locking/remote state
    # must never be adopted. After 35s, remain a quiet supervisor while Mutter
    # preserves its local PAM recovery path and retries lock confirmation.
    state = wait_startup_ready(lambda: native_call('GetState')[0],
        lambda: native_call('PrepareLogin', GLib.Variant('(o)', (session,))),
        read_status=lambda: native_call('GetStatus')[0], wait_for_recovery=True,
        heartbeat=watchdog.ping, log=lambda message: print(message, flush=True))
    if state == 'local' and not greeter and gateway_enabled and boot_guard_present():
        try:
            finish_local_reclaim(session, lambda: native_call('GetStatus')[0], boot_guard_present,
                                 heartbeat=watchdog.ping,
                                 log=lambda message: print(message, flush=True))
        except LocalReclaimRejected as error:
            # The local desktop remains usable after PAM recovery. A root
            # policy refusal must not produce a repeated backend restart storm.
            print(json.dumps({'event': 'native-local-reclaim-refused', 'reason': str(error)}), flush=True)
            return
    recover_monitor_layout_before_broker(state)
    gate = RUN / 'gate.sock'
    gate.unlink(missing_ok=True)  # exclusive supervisor lock; previous child is gone
    env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=os.environ.get('DBUS_SESSION_BUS_ADDRESS', f'unix:path=/run/user/{os.getuid()}/bus'))
    # Only this supervisor reports liveness to systemd.
    for key in ('NOTIFY_SOCKET', 'WATCHDOG_USEC', 'WATCHDOG_PID'):
        env.pop(key, None)
    broker = subprocess.Popen(['/usr/bin/python3', '-m', 'takeover.native_broker', '--socket', str(gate),
                               '--session', session] + (['--greeter'] if greeter else []), cwd=ROOT, env=env)
    children = [broker]

    try:
        deadline = time.monotonic() + 8
        while not gate.exists() and broker.poll() is None and time.monotonic() < deadline:
            watchdog.ping()
            time.sleep(.05)
        if not gate.exists():
            raise RuntimeError('Native broker did not start')
        # Migration from the legacy shared-workspace service is an explicit
        # operator action. Normal startup never stops unrelated entrypoints.
        retire_legacy_services('--retire-legacy-services' in sys.argv)
        grd_env = dict(env, DCONF_PROFILE=str(CONFIG / 'dconf-profile'), XDG_DATA_HOME=str(DATA / 'rdp'),
                       GRD_TAKEOVER_SOCKET=str(gate), GRD_NATIVE_TAKEOVER='1',
                       LD_LIBRARY_PATH=str(ROOT / 'vendor/grd-deps/root/usr/lib/x86_64-linux-gnu'))
        if gateway_enabled:
            grd_env['GRD_NATIVE_LOOPBACK'] = '1'
        if greeter:
            grd_env.update(greeter_env)
            grd_env['GRD_NATIVE_GREETER'] = '1'
        for key in ('GSETTINGS_BACKEND', 'GSETTINGS_SCHEMA_DIR', 'MUTTER_NATIVE_TAKEOVER',
                    'MUTTER_NATIVE_TAKEOVER_TEST'):
            grd_env.pop(key, None)
        grd = subprocess.Popen([str(ROOT / 'build/grd-prefix/libexec/gnome-remote-desktop-daemon'),
                                '--headless', f'--rdp-port={port}'], env=grd_env)
        children.append(grd)
        if gateway_enabled:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and all(p.poll() is None for p in children):
                watchdog.ping()
                if backend_listening(port, grd.pid):
                    break
                time.sleep(.1)
            else:
                raise RuntimeError('Private RDP backend failed to listen')
            registration = GatewayRegistration('/run/ubuntu-native-login/control.sock',
                'greeter' if greeter else 'desktop', session,
                log=lambda message: print(message, flush=True))
        print(json.dumps({'native_session': candidates[0], 'rdp_port': port, 'local_viewer': False,
                          'greeter':greeter, 'system_gateway':gateway_enabled}), flush=True)
        while all(p.poll() is None for p in children):
            watchdog.ping()
            if registration:
                registration.step()
            time.sleep(.25)
        report_child_exit(broker, grd)
    except KeyboardInterrupt:
        pass
    finally:
        if registration:
            registration.close()
        for p in reversed(children):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        gate.unlink(missing_ok=True)
        subprocess.run(['/usr/bin/python3', '-m', 'takeover.monitor_layout'], cwd=ROOT, check=False)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
