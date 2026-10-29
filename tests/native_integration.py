#!/usr/bin/env python3
"""Real patched Mutter + GRD on private buses/network, with injected lock faults.

The virtual output exercises the production stage/input/capture code. Physical
DRM presentation, physical libinput, PAM and Windows mstsc are separate checks.
"""
import json
import hashlib
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import shutil

import integration as harness
from gi.repository import Gio, GLib

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = Path(os.environ.get('NATIVE_TEST_RUNTIME_ROOT', str(ROOT)))
RESULTS = Path(os.environ.get('NATIVE_TEST_ARTIFACT_ROOT', str(ROOT / 'artifacts')))
ART = RESULTS / 'native-evidence'
RUN = Path(os.environ.get('NATIVE_TEST_RUN_DIR', str(ROOT / 'artifacts/native-runtime')))
SHELL = '--shell' in sys.argv
if '--gateway-handover-probe' in sys.argv:
    for option in ('--greeter-probe', '--handover-probe'):
        if option not in sys.argv:
            sys.argv.append(option)
if SHELL:
    ART = RESULTS / 'native-shell-evidence'
    RUN = Path(os.environ.get('NATIVE_TEST_RUN_DIR', str(ROOT / 'artifacts/native-shell-runtime')))
harness.ART = ART
spawn, stop, wait_for = harness.spawn, harness.stop, harness.wait_for
NAME = 'org.gnome.Mutter.NativeTakeover'
OBJ = '/org/gnome/Mutter/NativeTakeover'
SYNTHETIC_SECRETS = set()


def failure_diagnostics():
    # Whitelist only synthetic test process logs. Never dump environment,
    # credential databases, key files, HOME, or the runtime directory.
    for name in ('mutter', 'grd', 'broker', 'screen-saver', 'pipewire', 'NativeRDPTest',
                 'target-mutter', 'target-grd', 'target-broker'):
        path = ART / (name + '.log')
        if path.is_file():
            tail = '\n'.join(path.read_text(errors='replace').splitlines()[-30:])[-3000:]
            for value in SYNTHETIC_SECRETS:
                tail = tail.replace(value, '<synthetic-credential-redacted>')
            print(f'--- isolated test diagnostic: {name} ---\n{tail}', file=sys.stderr, flush=True)


def main():
    try:
        host_network = os.environ.get('NATIVE_TEST_HOST_NET_NS') or os.readlink('/proc/1/ns/net')
    except PermissionError:
        # Hardened procfs may hide PID 1 from the unprivileged test user.
        # The privileged launcher records this before unshare; still require
        # a distinct namespace rather than skipping the isolation check.
        host_network = os.environ.get('NATIVE_TEST_HOST_NET_NS')
    assert host_network and os.readlink('/proc/self/ns/net') != host_network, \
        'Use a private network namespace; record NATIVE_TEST_HOST_NET_NS before unshare when procfs hides PID 1'
    os.umask(0o077)
    ART.mkdir(parents=True, exist_ok=True)
    RUN.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = dict(os.environ, XDG_RUNTIME_DIR=str(RUN), XDG_CONFIG_HOME=str(RUN / 'config'),
               XDG_CACHE_HOME=str(RUN / 'cache'), XDG_DATA_HOME=str(RUN / 'data'),
               GSETTINGS_BACKEND='memory', LIBGL_ALWAYS_SOFTWARE='1', GSK_RENDERER='cairo')
    settings_file = RUN / 'config/ubuntu-native-takeover/remote-settings.json'
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text('{"scale_percent":100}')
    if '--ime' in sys.argv:
        assert SHELL
        extension = RUN / 'data/gnome-shell/extensions/kimpanel@kde.org'
        shutil.copytree(ROOT / 'build/kimpanel-fix', extension, dirs_exist_ok=True)
        entry = extension / 'extension.js'
        text = entry.read_text().replace('<method name="SetSpotRect">',
            '<method name="GetTestGeometry"><arg type="s" direction="out"/></method><method name="SetSpotRect">')
        text = text.replace('    SetSpotRect(x, y, w, h)', '''    GetTestGeometry() {
        const p = this.inputpanel.panel;
        const [x,y] = p.get_transformed_position();
        const [width,height] = p.get_transformed_size();
        const item = this.inputpanel.lookupTableLayout.get_children()[0];
        const [itemX,itemY] = item ? item.get_transformed_position() : [0,0];
        const [itemWidth,itemHeight] = item ? item.get_transformed_size() : [0,0];
        const m = Main.layoutManager.primaryMonitor;
        const point = global.get_pointer();
        const picked = global.stage.get_actor_at_pos(1, point[0], point[1]);
        return JSON.stringify({x,y,width,height,visible:p.visible,itemX,itemY,itemWidth,itemHeight,
            picked:String(picked), modalCount:Main.modalCount,
            overviewVisible:Main.overview.visible, overviewShown:Main.overview._shown,
            overviewAnimation:Main.overview.animationInProgress,
            shieldActive:Main.screenShield.active, shieldLocked:Main.screenShield.locked,
            windowRects:global.get_window_actors().map(a=>({title:a.meta_window.get_title(), rect:a.meta_window.get_frame_rect()})),
            pointer:global.get_pointer().slice(0,2),
            cursorWidth:this.inputpanel._cursor.width,
            cursorHeight:this.inputpanel._cursor.height,
            monitorWidth:m.width,monitorHeight:m.height});
    }
    SetSpotRect(x, y, w, h)''')
        entry.write_text(text)
    for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'SESSION_MANAGER',
                'PIPEWIRE_REMOTE', 'PIPEWIRE_RUNTIME_DIR', 'DCONF_PROFILE', 'GSETTINGS_SCHEMA_DIR'):
        env.pop(key, None)
    env['DBUS_SYSTEM_BUS_ADDRESS'] = 'unix:path=' + str(RUN / 'unavailable-system-bus')
    bus = subprocess.Popen(['dbus-daemon', '--session', '--nofork', '--print-address=1'], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    harness.children.append(bus)
    env['DBUS_SESSION_BUS_ADDRESS'] = bus.stdout.readline().strip()
    # A test compositor must never send Lock/Unlock or GDM registration to
    # the host system bus. The fake logind below lives on this private bus.
    env['DBUS_SYSTEM_BUS_ADDRESS'] = env['DBUS_SESSION_BUS_ADDRESS']
    env['XDG_SESSION_ID'] = 'test'
    spawn(['pipewire'], env, 'pipewire')
    wait_for(lambda: (RUN / 'pipewire-0').exists(), 'private PipeWire did not start')
    cfg = RUN / 'config/wireplumber/wireplumber.conf.d'
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / '99-no-hardware.conf').write_text('wireplumber.profiles = { main = { monitor.alsa = disabled monitor.bluez = disabled monitor.v4l2 = disabled monitor.libcamera = disabled } }')
    spawn(['wireplumber'], env, 'wireplumber')
    lock_config = RUN / 'lock-config.json'
    lock_config.write_text('{}')
    spawn(['/usr/bin/python3', str(ROOT / 'tests/native_screen_saver.py'), str(lock_config)] +
          (['--logind-only'] if SHELL else []), env, 'screen-saver')
    prefix = RUNTIME_ROOT / 'build/native-prefix'
    lib = prefix / 'lib/x86_64-linux-gnu'
    native_env = dict(env, LD_LIBRARY_PATH=f'{lib}:{lib}/mutter-18',
                      GI_TYPELIB_PATH=str(lib / 'mutter-18'),
                      MUTTER_NATIVE_TYPELIB_DIR=str(lib / 'mutter-18'),
                      MUTTER_NATIVE_TAKEOVER='1', MUTTER_NATIVE_TAKEOVER_TEST='1')
    auth_policy = RUN / 'auth-policy'
    auth_policy.write_text('reject')
    fake_auth = RUN / 'fake-local-auth'
    fake_auth.write_text('#!/usr/bin/python3\nimport sys,time\nfrom pathlib import Path\np=sys.stdin.buffer.read()\npolicy=Path('+repr(str(auth_policy))+').read_text()\nif policy == "delayed": time.sleep(2)\nsys.exit(0 if policy in ("accept", "delayed") and p == b"a" else 1)\n')
    fake_auth.chmod(0o700)
    if '--boot-guard' in sys.argv:
        native_env['MUTTER_NATIVE_BOOT_PRIVACY'] = '1'
    if '--greeter-probe' in sys.argv:
        native_env['MUTTER_NATIVE_GREETER'] = '1'
    native_env['MUTTER_NATIVE_TEST_AUTH_HELPER'] = str(fake_auth)
    native_env['MUTTER_NATIVE_TEST_FRAME'] = str(ART / 'physical-local-lock.png')
    command = ['/usr/bin/gnome-shell'] if SHELL else [str(prefix / 'bin/mutter')]
    if SHELL and '--nx' in sys.argv:
        command = ['/usr/NX/scripts/env/nxpreload.sh', '/usr/bin/gnome-shell', '--mode=ubuntu']
    command += ['--headless', '--no-x11', '--wayland', '--wayland-display=native-test', '--virtual-monitor=1280x720']
    if '--two-monitors' in sys.argv:
        command += ['--virtual-monitor=1920x1080' if '--unequal-monitors' in sys.argv
                    else '--virtual-monitor=1280x720']
    if not SHELL:
        command += ['--mutter-plugin=' + str(lib / 'mutter-18/plugins/libdefault.so')]
    if '--trace-loader' in sys.argv:
        command = ['strace', '-f', '-e', 'trace=openat', '-o', str(ART / 'loader.log'), *command]
    compositor = spawn(command, native_env, 'mutter')
    wait_for(lambda: (RUN / 'native-test').exists(), 'patched Mutter failed')

    def connect():
        return Gio.DBusConnection.new_for_address_sync(env['DBUS_SESSION_BUS_ADDRESS'],
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
    observer = connect()

    def call(method, params=None, connection=None):
        return (connection or observer).call_sync(NAME, OBJ, NAME, method, params, None, 0, 8000, None).unpack()

    def inspect_local():
        physical = call('InspectPhysical')
        from PIL import Image
        with Image.open(ART / 'physical-local-lock.png') as image:
            # The password field must survive tiny remote app/clock damage.
            cx, cy = image.width // 2, image.height // 2
            assert min(image.getpixel((cx - 190, cy + 40))[:3]) > 30, 'physical password field was clipped away'
            assert min(image.getpixel((cx - 340, cy + 250))[:3]) > 10, 'physical authentication surface was only partially drawn'
        return physical

    def ready():
        try:
            return call('GetState')[0] == ('boot-guard' if '--boot-guard' in sys.argv else 'local')
        except GLib.Error:
            return False
    wait_for(ready, 'native privacy service not ready')
    if '--boot-guard' in sys.argv:
        if SHELL:
            wait_for(lambda: 'GNOME Shell started' in (ART / 'mutter.log').read_text(), 'Shell did not start behind guard')
        guard_app = spawn(['/usr/bin/python3', '-c',
            "import gi;gi.require_version('Gtk','4.0');from gi.repository import Gtk;"
            "a=Gtk.Application(application_id='local.takeover.GuardProbe');"
            "a.connect('activate',lambda a:(lambda w:(w.set_default_size(900,600),w.set_child(Gtk.Label(label='PRIVATE DESKTOP MUST NOT BE VISIBLE')),w.present()))(Gtk.ApplicationWindow(application=a)));a.run([])"],
            dict(env, WAYLAND_DISPLAY='native-test', GDK_BACKEND='wayland'), 'guard-probe')
        time.sleep(2)
        assert guard_app.poll() is None
        assert call('InspectPhysical') == (2 if '--two-monitors' in sys.argv else 1, True), 'Startup leaked the private desktop'
        stop(guard_app)
        call('PrepareLogin', GLib.Variant('(o)', ('/test',)))
        wait_for(lambda: call('GetState') == ('ready-guarded',), 'Startup guard did not confirm lock', 25)
    if '--exact-viewport' in sys.argv:
        sys.path.insert(0, str(ROOT))
        from takeover.monitor_layout import MonitorLayout, remote_monitor, snapshot
        viewport_layout = MonitorLayout(observer, RUN / 'unused-test-journal')
        initial_monitor_state = viewport_layout.state()
        assert initial_monitor_state[3].get('native-takeover-viewport-supported')
        target, _ = remote_monitor(initial_monitor_state, 2560, 1440)
        try:
            viewport_layout.apply(initial_monitor_state, target, method=0)
        except GLib.Error:
            pass
        else:
            raise AssertionError('Unprotected local viewport request was accepted')
    if SHELL:
        wait_for(lambda: 'GNOME Shell started' in (ART / 'mutter.log').read_text(), 'real GNOME Shell did not finish startup')
    loaded_files = {}
    for line in Path(f'/proc/{compositor.pid}/maps').read_text().splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) == 6 and fields[5].startswith('/'):
            mapped = Path(fields[5])
            if mapped.is_file() and (str(mapped).startswith(str(lib)) or
                                    mapped.name in ('gnome-shell', 'libshell-18.so')):
                loaded_files[str(mapped.resolve())] = hashlib.sha256(mapped.read_bytes()).hexdigest()
    if '--ime' in sys.argv:
        result = observer.call_sync('org.gnome.Shell', '/org/gnome/Shell',
            'org.gnome.Shell.Extensions', 'EnableExtension', GLib.Variant('(s)', ('kimpanel@kde.org',)),
            None, 0, 5000, None).unpack()
        assert result == (True,), 'Isolated Input Method Panel could not be enabled'
    state_file = ART / 'app-state.json'
    state_file.unlink(missing_ok=True)
    viewport_control = RUN / 'viewport-control'
    viewport_control.unlink(missing_ok=True)
    app = spawn(['/usr/bin/python3', str(ROOT / 'tests/desktop_app.py')],
                dict(env, WAYLAND_DISPLAY='native-test', GDK_BACKEND='wayland',
                     TAKEOVER_TEST_POINTER_DEBUG='1',
                     TAKEOVER_TEST_APP_STATE=str(state_file),
                     TAKEOVER_TEST_VIEWPORT_CONTROL=str(viewport_control)
                     if '--exact-viewport' in sys.argv else ''), 'app')
    wait_for(state_file.exists, 'Wayland application failed')
    time.sleep(1)
    if '--boot-guard' in sys.argv:
        locked = observer.call_sync('org.gnome.ScreenSaver', '/org/gnome/ScreenSaver',
            'org.gnome.ScreenSaver', 'GetActive', None, None, 0, 3000, None).unpack()[0]
        assert locked and call('GetState') == ('ready-guarded',), 'Startup did not finish at a locked desktop'
    else:
        assert call('InspectPhysical') == (2 if '--two-monitors' in sys.argv else 1, False), 'baseline native output should contain the app'
    fd, out = os.pipe()
    spawn(['Xvfb', '-displayfd', str(out), '-screen', '0',
           '3000x1800x24' if '--exact-viewport' in sys.argv else '1400x900x24', '-nolisten', 'tcp'],
          env, 'xvfb', pass_fds=(out,))
    os.close(out)
    with os.fdopen(fd) as f:
        display = ':' + f.readline().strip()
    client_env = dict(env, DISPLAY=display, GDK_BACKEND='x11',
        LD_LIBRARY_PATH=str(ROOT / 'vendor/ubuntu-freerdp/root/usr/lib/x86_64-linux-gnu'))
    gate = RUN / 'native-gate.sock'
    gate.unlink(missing_ok=True)

    greeter_options = []
    redirect_ready = None
    production_gateway = None
    source_stopped_before_target = False
    proxy_port = [43394]
    if '--gateway-handover-probe' in sys.argv:
        from gateway_handover_fixture import ProductionGatewayFixture
        production_gateway = ProductionGatewayFixture(RUNTIME_ROOT, RUN / 'production-gateway')
        greeter_options = ['--greeter', '--gateway', str(production_gateway.path)]
    elif '--greeter-probe' in sys.argv:
        import threading
        fake_gateway = RUN / 'test-gateway.sock'
        fake_gateway.unlink(missing_ok=True)
        gateway_socket = socket.socket(socket.AF_UNIX)
        gateway_socket.bind(str(fake_gateway));gateway_socket.listen()
        redirect_ready = threading.Event()
        def gateway_worker():
            while True:
                connection,_ = gateway_socket.accept()
                with connection:
                    request=json.loads(connection.makefile('rb').readline())
                    assert request['command'] == 'lease' and request['role'] == 'greeter'
                    connection.sendall(b'GUARDED\n')
                    if '--handover-probe' in sys.argv:
                        redirect_ready.wait(40)
                        connection.sendall(b'HANDOVER\n')
                    connection.recv(4096)
        threading.Thread(target=gateway_worker, daemon=True).start()
        greeter_options = ['--greeter', '--gateway', str(fake_gateway)]
    def start_broker():
        p = spawn(['/usr/bin/python3', '-m', 'takeover.native_broker', '--socket', str(gate),
                   '--session', '/org/freedesktop/login1/session/test'] + greeter_options, env, 'broker', cwd=RUNTIME_ROOT)
        wait_for(gate.exists, 'native broker failed')
        return p

    broker = start_broker()
    cert, key = RUN / 'rdp.crt', RUN / 'rdp.key'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-subj', '/CN=native-isolated-test', '-keyout', str(key), '-out', str(cert)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    password = secrets.token_urlsafe(24)
    SYNTHETIC_SECRETS.add(password)
    grd_env = dict(env, GSETTINGS_BACKEND='keyfile', GRD_TAKEOVER_SOCKET=str(gate), GRD_NATIVE_TAKEOVER='1',
        GNOME_REMOTE_DESKTOP_TEST_RDP_USERNAME='native-test', GNOME_REMOTE_DESKTOP_TEST_RDP_PASSWORD=password,
        LD_LIBRARY_PATH=str(ROOT / 'vendor/grd-deps/root/usr/lib/x86_64-linux-gnu' if RUNTIME_ROOT == ROOT else RUNTIME_ROOT / 'build/grd-prefix/lib/x86_64-linux-gnu'))
    if '--greeter-probe' in sys.argv:
        grd_env['GRD_NATIVE_GREETER'] = '1'
    for schema, name, value in [('org.gnome.desktop.remote-desktop.rdp', 'tls-cert', str(cert)),
                               ('org.gnome.desktop.remote-desktop.rdp', 'tls-key', str(key)),
                               ('org.gnome.desktop.remote-desktop.rdp.headless', 'enable', 'true')]:
        subprocess.run(['gsettings', 'set', schema, name, value], env=grd_env, check=True)
    grd = spawn([str(RUNTIME_ROOT / 'build/grd-prefix/libexec/gnome-remote-desktop-daemon'),
                 '--headless', '--rdp-port=43394' if '--handover-probe' in sys.argv else '--rdp-port=43392'], grd_env, 'grd')
    wait_for(lambda: 'RDP server started' in (ART / 'grd.log').read_text(), 'native GRD failed')
    if production_gateway:
        production_gateway.register_source()
    if '--negotiation-probe' in sys.argv:
        import struct
        negotiation = {}
        for protocols in (0xb, 0x3, 0x1):
            with socket.create_connection(('127.0.0.1', 43394 if '--handover-probe' in sys.argv else 43392), timeout=3) as probe:
                probe.sendall(bytes.fromhex('030000130ee0000000000001000800') + struct.pack('<I', protocols))
                response = b''
                while len(response) < 19:
                    part = probe.recv(19 - len(response))
                    if not part:
                        raise RuntimeError('RDP negotiation probe closed early')
                    response += part
                kind, value = response[11], int.from_bytes(response[15:19], 'little')
                assert (kind, value) == ((3, 5) if protocols == 1 else (2, 2)), \
                    'NLA-only HYBRID/mac compatibility negotiation changed'
                negotiation[hex(protocols)] = {'type': kind, 'value': value}
        assert call('GetState') == ('local',), 'Unauthenticated negotiation changed native privacy state'
        (RESULTS / 'grd-negotiation-result.json').write_text(json.dumps(negotiation, indent=2) + '\n')
    if '--handover-probe' in sys.argv and not production_gateway:
        import select
        listener=socket.socket();listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        listener.bind(('127.0.0.1',43392));listener.listen()
        def relay(connection):
            with connection, socket.create_connection(('127.0.0.1',proxy_port[0])) as target:
                while True:
                    readable,_,_=select.select([connection,target],[],[],30)
                    if not readable:return
                    for source in readable:
                        try:
                            data=source.recv(65536)
                            if not data:return
                            (target if source is connection else connection).sendall(data)
                        except (ConnectionError, OSError):
                            return
        def accept():
            while True:
                connection,_=listener.accept()
                threading.Thread(target=relay,args=(connection,),daemon=True).start()
        threading.Thread(target=accept,daemon=True).start()

    def remote(title, pwd=password):
        SYNTHETIC_SECRETS.add(pwd)
        return spawn([os.environ.get('NATIVE_TEST_FREERDP', str(ROOT / 'vendor/ubuntu-freerdp/root/usr/bin/xfreerdp')), '/v:127.0.0.1:43392',
                      '/u:native-test', '/p:' + pwd, '/cert:ignore', '/size:1280x720', '/gfx:RFX',
                      '/network:lan', '/title:' + title] +
                     (['/dynamic-resolution'] if '--adaptive' in sys.argv else []), client_env, title)

    bad = remote('NativeBadPassword', 'invalid-' + secrets.token_urlsafe(8))
    wait_for(lambda: bad.poll() is not None, 'bad password did not fail')
    assert call('GetState') == (('ready-guarded',) if '--boot-guard' in sys.argv else ('local',))
    def monitor_count():
        return len(connect().call_sync('org.gnome.Mutter.DisplayConfig',
            '/org/gnome/Mutter/DisplayConfig', 'org.gnome.Mutter.DisplayConfig',
            'GetCurrentState', None, None, 0, 3000, None).unpack()[2])
    if '--two-monitors' in sys.argv:
        assert monitor_count() == 2
    # Simulate hardware that never releases modifiers, consumer keys and a drag.
    # The real seat must be neutral before the remote client can type/click.
    if '--boot-guard' not in sys.argv:
        call('TestHoldInput')
        assert call('TestInputState')[0] == 6
    else:
        assert call('TestInputState')[0] == 0, 'Startup privacy admitted physical input'
    first = remote('NativeRDPTest')
    wait_for(lambda: call('GetState') == ('remote',), 'native RDP takeover failed', 20)
    if '--two-monitors' in sys.argv:
        wait_for(lambda: monitor_count() == 1, 'remote layout was not applied')
    if '--greeter-probe' in sys.argv:
        window = harness.wait_for_client_window(first, client_env, 'NativeRDPTest')
        for command in (['windowfocus',window], ['type','--clearmodifiers','greeter-input-over-rdp']):
            subprocess.run(['xdotool',*command],env=client_env,check=True)
        wait_for(lambda: json.loads(state_file.read_text())['text'] == 'greeter-input-over-rdp', 'Greeter RDP input failed')
        assert call('InspectPhysical') == (2 if '--two-monitors' in sys.argv else 1, True), 'Greeter input was visible physically'
        if '--handover-probe' in sys.argv:
            from handover_target import launch
            if production_gateway:
                # Reproduce real GDM ordering: its authenticated user session
                # appears before target GRD readiness; the source then exits.
                production_gateway.create_target_session()
                wait_for(lambda: 'Sending server redirection' in (ART / 'grd.log').read_text(),
                         'Production gateway did not request early RDP redirection', 5)
                wait_for(lambda: production_gateway.gateway.waiting_relays > 0,
                         'Redirected FreeRDP client did not open a fresh waiting connection', 5)
                assert 'desktop' not in production_gateway.gateway.endpoints
                assert (production_gateway.directory / 'boot-guard').exists()
                stop(grd)
                stop(broker)
                production_gateway.remove_source()
                source_stopped_before_target = grd.poll() is not None
                assert source_stopped_before_target
                time.sleep(1)  # Deliberately leave the redirected handshake pending.
            target_state,target_app=launch(env,prefix,RUN/'handover-target',
                'incorrect-'+password if '--reject-handover' in sys.argv else password,cert,key)
            if production_gateway:
                production_gateway.register_target()
            else:
                proxy_port[0]=43393
                redirect_ready.set()
            if '--reject-handover' in sys.argv:
                wait_for(lambda:first.poll() is not None,'Wrong RDSTLS password was not rejected',20)
                assert target_state()=='local','Wrong RDSTLS credentials acquired the native desktop'
                assert target_state.grd.poll() is None,'RDSTLS rejection crashed the target server'
                result={'rdstls_wrong_password_rejected':True,'native_desktop_not_granted':True}
                if production_gateway:
                    result.update(production_gateway=True, source_stopped_before_target_ready=source_stopped_before_target,
                                  real_gdm_pam_tested=False)
                    production_gateway.close()
                (RESULTS/'greeter-auth-rejection-result.json').write_text(json.dumps(result,indent=2)+'\n')
                print(json.dumps(result),flush=True)
                return
            def target_ready():
                assert target_state.grd.poll() is None, f'Target RDP exited: {target_state.grd.returncode}'
                return target_state() == 'remote'
            wait_for(target_ready, 'RDP client did not follow native handover', 25)
            assert first.poll() is None, 'Handover terminated client process'
            time.sleep(3)  # Wait for the redirected client's capture/input setup, as for the initial client.
            window = harness.wait_for_client_window(first, client_env, 'NativeRDPTest')
            subprocess.run(['xdotool','windowfocus',window],env=client_env,check=True)
            subprocess.run(['xdotool','type','--clearmodifiers','native-after-login'],env=client_env,check=True)
            wait_for(lambda: json.loads(target_app.read_text())['text']=='native-after-login','Redirected input did not reach native desktop')
        stop(first)
        wait_for(lambda: call('GetState') == ('local',), 'Greeter disconnect did not restore login screen')
        result={'greeter_rdp_input':True,'physical_greeter_black':True,'disconnect_restores_greeter':True,
                'real_gdm_pam_tested':False, 'automatic_rdp_handover_verified':'--handover-probe' in sys.argv}
        if production_gateway:
            result.update(production_gateway=True, source_stopped_before_target_ready=source_stopped_before_target,
                          fresh_client_handshake_waited_for_target=True,
                          gateway_sha256=hashlib.sha256((RUNTIME_ROOT / 'takeover/login_gateway.py').read_bytes()).hexdigest())
            production_gateway.close()
        result['tested_binaries'] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in
            [lib / 'libmutter-18.so.0', RUNTIME_ROOT / 'build/grd-prefix/libexec/gnome-remote-desktop-daemon']}
        (RESULTS/'greeter-integration-result.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result),flush=True)
        return
    assert inspect_local() == (2 if '--two-monitors' in sys.argv else 1, False), 'physical authentication UI was not drawn'
    if '--two-monitors' in sys.argv:
        assert monitor_count() == 1, 'secondary output remained outside the RDP frame'
    assert call('TestInputState') == (0, 0, False), 'physical modifiers/buttons/repeat survived takeover'

    def xd(*args):
        return subprocess.check_output(['xdotool', *args], env=client_env, text=True).strip()
    wid = harness.wait_for_client_window(first, client_env, 'NativeRDPTest')
    xd('windowfocus', wid)
    xd('type', '--clearmodifiers', '--delay', '60', 'native-original-session-123')
    from PIL import ImageGrab
    ImageGrab.grab(xdisplay=display).save(ART / 'rdp-original.png')
    wait_for(lambda: json.loads(state_file.read_text())['text'] == 'native-original-session-123',
             'RDP input did not reach original application')
    assert json.loads(state_file.read_text())['pid'] == app.pid
    if SHELL and '--boot-guard' in sys.argv:
        # The startup Overview can return after the first unlock. The initial
        # client key may precede input setup; do this after verified typing.
        xd('key', 'Super_L')
        time.sleep(1)
    xd('mousemove', '--window', wid, '450', '380')
    time.sleep(.3)
    xd('mousemove_relative', '--', '1', '0')
    time.sleep(.3)  # Xvfb warp reports the previous position until the next motion.
    if '--ime' in sys.argv:
        (ART / 'initial-pointer.json').write_text(observer.call_sync(
            'org.kde.impanel', '/org/kde/impanel', 'org.kde.impanel2',
            'GetTestGeometry', None, None, 0, 3000, None).unpack()[0])
    xd('mousedown', '1')
    time.sleep(.3)
    xd('mouseup', '1')
    wait_for(lambda: json.loads(state_file.read_text())['clicks'] == 1, 'RDP mouse did not reach original application')
    assert inspect_local() == (2 if '--two-monitors' in sys.argv else 1, False), 'physical authentication UI disappeared'
    def local_key(key):
        call('TestLocalKey', GLib.Variant('(ub)', (key, True)))
        call('TestLocalKey', GLib.Variant('(ub)', (key, False)))
    local_key(30)  # A, routed solely into the local authentication keymap.
    local_key(28)  # Enter
    time.sleep(.5)
    assert first.poll() is None and call('GetState') == ('remote',), 'wrong local password revoked remote'
    assert json.loads(state_file.read_text())['text'] == 'native-original-session-123', 'local password leaked into remote app'
    if '--adaptive' in sys.argv:
        if '--exact-viewport' in sys.argv:
            for size, method in [((2560, 1440), 2), ((0, 600), 0), ((8193, 600), 0)]:
                current = viewport_layout.state()
                target, _ = remote_monitor(current, 2560, 1440)
                target['properties']['native-takeover-size'] = list(size)
                try:
                    viewport_layout.apply(current, target, method=method)
                except GLib.Error:
                    pass
                else:
                    raise AssertionError('Persistent or invalid viewport request was accepted')
            if '--remote-scale' in sys.argv:
                for invalid in (0.0, 1.1, 2.5, float('nan'), float('inf')):
                    current = viewport_layout.state()
                    target, _ = remote_monitor(current, 2560, 1440)
                    target['logical'][0][2] = invalid
                    try:
                        viewport_layout.apply(current, target, method=0)
                    except GLib.Error:
                        pass
                    else:
                        raise AssertionError(f'Invalid remote scale accepted: {invalid}')
            xd('windowsize', wid, '2560', '1440')
            wait_for(lambda: '"selected": [2560, 1440], "exact": true' in (ART / 'broker.log').read_text(),
                     'Fullscreen 2560x1440 was restricted by physical modes', 10)
            current = viewport_layout.state()
            assert tuple(current[3]['native-takeover-size']) == (2560, 1440)
            hardware_modes = lambda state: {s[0]: next(m[0] for m in modes if m[6].get('is-current'))
                                            for s, modes, _ in state[1]}
            assert hardware_modes(current) == hardware_modes(initial_monitor_state), 'Remote resize changed a physical hardware mode'
            if '--remote-scale' in sys.argv:
                for percent in [125, 175, 200, 150]:
                    with socket.socket(socket.AF_UNIX) as control:
                        control.settimeout(8)
                        control.connect(str(gate))
                        control.sendall(f'SCALE {percent}\n'.encode())
                        result = json.loads(control.makefile('rb').readline())
                    assert result.get('ok'), result
                    scaled = viewport_layout.state()
                    assert scaled[2][0][2] == percent / 100
                    assert tuple(scaled[3]['native-takeover-size']) == (2560, 1440)
                    assert first.poll() is None, 'Scale change disconnected the RDP client'
            viewport_control.touch()
            time.sleep(3)
            before = ImageGrab.grab(xdisplay=display)
            before.save(ART / 'rdp-2560-before.png')
            xd('mousemove', '--window', wid, '2440', '1380')
            xd('click', '1')
            wait_for(lambda: json.loads(state_file.read_text())['clicks'] == 2,
                     'Mouse input outside the physical 1280x720 area failed')
            time.sleep(2)
            after = ImageGrab.grab(xdisplay=display)
            after.save(ART / 'rdp-2560-after.png')
            assert before.crop((2300, 1300, 2550, 1440)).tobytes() != after.crop((2300, 1300, 2550, 1440)).tobytes(), 'Remote edge damage was not painted'
            if '--ime' in sys.argv:
                def panel(method, args=None):
                    return observer.call_sync('org.kde.impanel', '/org/kde/impanel',
                        'org.kde.impanel2', method, args, None, 0, 5000, None).unpack()
                panel('SetRelativeSpotRectV2', GLib.Variant('(iiiid)', (500, 160, 2, 30, 1.5)))
                panel('SetLookupTable', GLib.Variant('(asasasbbii)',
                    (['1.', '2.'], ['测试候选一', '测试候选二'], ['', ''], False, False, 0, 0)))
                observer.emit_signal(None, '/org/kde/impanel', 'org.kde.kimpanel.inputmethod',
                                     'ShowLookupTable', GLib.Variant('(b)', (True,)))
                time.sleep(1)
                geometry = json.loads(panel('GetTestGeometry')[0])
                assert geometry['visible'] and geometry['cursorWidth'] > 0 and geometry['cursorHeight'] > 0, geometry
                assert 0 <= geometry['x'] <= geometry['monitorWidth'] - geometry['width'], geometry
                assert 0 <= geometry['y'] <= geometry['monitorHeight'] - geometry['height'], geometry
                ImageGrab.grab(xdisplay=display).save(ART / 'rdp-ime-150.png')
                (ART / 'ime-geometry.json').write_text(json.dumps(geometry))
                selected = []
                watch = observer.signal_subscribe(None, 'org.kde.impanel', 'SelectCandidate',
                    '/org/kde/impanel', None, Gio.DBusSignalFlags.NONE,
                    lambda *args: selected.append(args[5].unpack()[0]))
                # Click the center of the first candidate, in RDP pixel coordinates.
                xd('mousemove', '--window', wid, str(round((geometry['itemX'] + geometry['itemWidth'] / 2) * 1.5)),
                   str(round((geometry['itemY'] + geometry['itemHeight'] / 2) * 1.5)))
                # Xvfb/FreeRDP reports a warped pointer one motion behind.
                xd('mousemove_relative', '--', '1', '0')
                time.sleep(1)
                xd('mousedown', '1')
                time.sleep(.3)
                xd('mouseup', '1')
                def candidate_selected():
                    context = GLib.MainContext.default()
                    while context.pending(): context.iteration(False)
                    return bool(selected)
                wait_for(candidate_selected, 'Candidate click did not reach input method')
                assert selected[0] == 0, selected
                observer.signal_unsubscribe(watch)
                observer.emit_signal(None, '/org/kde/impanel', 'org.kde.kimpanel.inputmethod',
                                     'ShowLookupTable', GLib.Variant('(b)', (False,)))
                (ART / 'ime-result.json').write_text(json.dumps({'geometry':geometry,'selected_candidate':selected[0]}))
            assert inspect_local() == (2 if '--two-monitors' in sys.argv else 1, False)
            if SHELL:
                viewport_control.write_text('windowed')
                time.sleep(1)
                xd('key', 'Super_L+Right')
                time.sleep(2)
                before = ImageGrab.grab(xdisplay=display)
                before.save(ART / 'rdp-right-half-before.png')
                xd('mousemove', '--window', wid, '2440', '1380')
                xd('click', '1')
                wait_for(lambda: json.loads(state_file.read_text())['clicks'] == 3,
                         'Window outside every physical view stopped accepting input')
                time.sleep(2)
                after = ImageGrab.grab(xdisplay=display)
                after.save(ART / 'rdp-right-half-after.png')
                assert before.crop((2300, 1300, 2550, 1440)).tobytes() != after.crop((2300, 1300, 2550, 1440)).tobytes(), 'Window outside physical views stopped painting'
        xd('windowsize', wid, '1000', '600')
        wait_for(lambda: '"requested": [1000, 600]' in (ART / 'broker.log').read_text(),
                 'Dynamic RDP display-control request did not reach broker', 10)
        if '--exact-viewport' in sys.argv:
            wait_for(lambda: tuple(viewport_layout.state()[3].get('native-takeover-size', ())) == (1000, 600),
                     'Small remote viewport did not apply')
            time.sleep(.5)
            assert inspect_local() == (2 if '--two-monitors' in sys.argv else 1, False)
        assert first.poll() is None and app.poll() is None
    time.sleep(11)
    assert first.poll() is None and call('GetState') == ('remote',), 'healthy native lease timed out'
    competitor = remote('NativeCompetitor')
    wait_for(lambda: competitor.poll() is not None, 'competing lease was not refused')
    assert call('GetState') == ('remote',)
    # Valid local authentication must terminate RDP before restoring the same
    # desktop, without asking for the password again on GNOME ScreenShield.
    auth_policy.write_text('accept')
    local_key(30)
    local_key(28)
    wait_for(lambda: call('GetState') == ('local',), 'valid local authentication did not reclaim')
    wait_for(lambda: first.poll() is not None, 'local reclaim left remote connected')
    saver_active = connect().call_sync('org.gnome.ScreenSaver', '/org/gnome/ScreenSaver',
        'org.gnome.ScreenSaver', 'GetActive', None, None, 0, 3000, None).unpack()[0]
    assert not saver_active, 'local reclaim asked for a second password'
    assert app.poll() is None and json.loads(state_file.read_text())['text'] == 'native-original-session-123'
    first = remote('NativeAfterLocalReclaim')
    wait_for(lambda: call('GetState') == ('remote',), 'RDP reconnect after local reclaim failed', 20)
    auth_policy.write_text('delayed')
    local_key(30)
    local_key(28)
    stop(first)
    wait_for(lambda: call('GetState') == ('local',), 'disconnect during local PAM did not lock')
    first = remote('NativeAfterCancelledAuth')
    wait_for(lambda: call('GetState') == ('remote',), 'reconnect after cancelled local auth failed', 20)
    time.sleep(2.3)
    assert call('GetState') == ('remote',) and first.poll() is None, 'stale authentication reclaimed a newer lease'
    stop(first)
    wait_for(lambda: call('GetState') == ('local',), 'disconnect did not restore confirmed lock')
    if '--two-monitors' in sys.argv:
        wait_for(lambda: monitor_count() == 2, 'disconnect did not restore both monitors')
    assert app.poll() is None and json.loads(state_file.read_text())['text'] == 'native-original-session-123'
    second = remote('NativeReconnect')
    wait_for(lambda: call('GetState') == ('remote',), 'reconnect failed', 20)
    if '--two-monitors' in sys.argv:
        wait_for(lambda: monitor_count() == 1, 'reconnected layout was not applied')
    assert inspect_local() == (2 if '--two-monitors' in sys.argv else 1, False)
    broker.kill()
    broker.wait()
    wait_for(lambda: second.poll() is not None, 'broker crash left RDP connected')
    wait_for(lambda: call('GetState') == ('local',), 'broker crash did not restore lock')
    if '--two-monitors' in sys.argv:
        # The production supervisor invokes this after detecting child exit.
        subprocess.run(['/usr/bin/python3', '-m', 'takeover.monitor_layout',
                        '--journal', str(gate.parent / 'monitor-layout.json')],
                       cwd=RUNTIME_ROOT, env=env, check=True)
        assert monitor_count() == 2, 'crash journal recovery did not restore both monitors'
        if '--exact-viewport' in sys.argv:
            assert snapshot(viewport_layout.state()) == snapshot(initial_monitor_state), 'Local layout/scales were not restored exactly'

    if not SHELL:
        # Lock failure must retain privacy indefinitely, not time out open.
        owner = connect()
        call('Begin', GLib.Variant('(o)', ('/test',)), owner)
        lock_config.write_text('{"fail_lock":true}')
        call('End', connection=owner)
        time.sleep(.5)
        assert call('GetState') == ('locking',) and call('InspectPhysical') == (2 if '--two-monitors' in sys.argv else 1, True)
        lock_config.write_text('{}')
        call('End', connection=owner)
        wait_for(lambda: call('GetState') == ('local',), 'retrying lock did not recover')
        owner.close_sync(None)
        # Kill the lease owner while Shell's asynchronous unlock is pending.
        from concurrent.futures import ThreadPoolExecutor
        lock_config.write_text('{"unlock_delay_ms":1000}')
        owner = connect()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(call, 'Begin', GLib.Variant('(o)', ('/test',)), owner)
            wait_for(lambda: call('GetState') == ('blanking',), 'delayed Begin did not start')
            time.sleep(.2)
            owner.close_sync(None)
            time.sleep(.2)
            assert call('GetState') == ('locking',) and call('InspectPhysical') == (2 if '--two-monitors' in sys.argv else 1, True)
            wait_for(lambda: call('GetState') == ('local',), 'owner loss during delayed unlock did not recover')
        lock_config.write_text('{}')
    assert app.poll() is None and compositor.poll() is None
    result = {'same_compositor_pid': compositor.pid, 'original_app_pid': app.pid,
              'rdp_input_to_original_app': True, 'held_input_takeover_neutralizes_real_seat': '--boot-guard' not in sys.argv,
              'physical_authentication_ui_with_live_remote_scene': True,
              'wrong_local_password_keeps_remote': True, 'local_password_not_delivered_to_remote_app': True,
              'verified_local_reclaim_revokes_remote_without_second_password': True,
              'stale_local_authentication_cannot_reclaim_new_lease': True,
              'mouse_to_original_app': True, 'idle_lease_survives_handshake_timeout': True,
              'incorrect_password_no_takeover': True, 'competing_lease_rejected': True,
              'disconnect_locks_before_local': True, 'broker_crash_recovers_lock': True,
              'failed_lock_stays_black': not SHELL, 'reconnect_preserves_original_app': True,
              'owner_loss_during_async_unlock_safe': not SHELL,
              'output_backend': 'headless virtual output using patched native Mutter code',
              'lock_backend': (subprocess.check_output(['/usr/bin/gnome-shell', '--version'], text=True).strip() + ' ScreenShield') if SHELL else 'fault-injected test ScreenSaver',
              'logind_locked_hint_checked': False,
              'physical_drm_libinput_tested': False, 'physical_pam_tested': False, 'windows_mstsc_tested': False}
    result['system_nx_wrapper_tested'] = '--nx' in sys.argv
    result['single_monitor_layout_round_trip'] = '--two-monitors' in sys.argv
    result['adaptive_display_control_verified'] = '--adaptive' in sys.argv
    result['exact_viewport_and_edge_input_verified'] = '--exact-viewport' in sys.argv
    result['unequal_physical_modes_verified'] = '--unequal-monitors' in sys.argv
    result['startup_guard_verified'] = '--boot-guard' in sys.argv
    result['remote_scale_switching_verified'] = '--remote-scale' in sys.argv
    result['local_scale_restoration_verified'] = '--exact-viewport' in sys.argv and '--two-monitors' in sys.argv
    result['input_method_panel_verified'] = '--ime' in sys.argv
    result['tested_binaries'] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in
        [lib / 'libmutter-18.so.0', RUNTIME_ROOT / 'build/grd-prefix/libexec/gnome-remote-desktop-daemon']}
    result['loaded_files'] = loaded_files
    target = 'native-shell-integration-result.json' if SHELL else 'native-integration-result.json'
    if RUNTIME_ROOT != ROOT:
        target = 'package-' + target
    (RESULTS / target).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        failure_diagnostics()
        raise
    finally:
        for p in reversed(harness.children):
            stop(p)
        for f in harness.handles:
            f.close()
        for name in ('rdp.key', 'rdp.crt', 'native-gate.sock'):
            (RUN / name).unlink(missing_ok=True)
