"""Second isolated native desktop for the real RDP server-redirection test."""
import json
import os
from pathlib import Path
import subprocess
import time
from gi.repository import Gio
import integration as harness

ROOT = Path(__file__).resolve().parents[1]


def launch(env, prefix, run, password, cert, key):
    runtime_root=prefix.parent.parent
    run.mkdir(mode=0o700, exist_ok=True)
    env=dict(env,XDG_RUNTIME_DIR=str(run),XDG_CONFIG_HOME=str(run/'config'),
             XDG_CACHE_HOME=str(run/'cache'),XDG_DATA_HOME=str(run/'data'))
    bus=subprocess.Popen(['dbus-daemon','--session','--nofork','--print-address=1'],env=env,
                         stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
    harness.children.append(bus)
    env['DBUS_SESSION_BUS_ADDRESS']=bus.stdout.readline().strip()
    env['DBUS_SYSTEM_BUS_ADDRESS']=env['DBUS_SESSION_BUS_ADDRESS']
    harness.spawn(['pipewire'],env,'target-pipewire')
    harness.wait_for(lambda:(run/'pipewire-0').exists(),'Target PipeWire missing')
    cfg=run/'config/wireplumber/wireplumber.conf.d';cfg.mkdir(parents=True,exist_ok=True)
    (cfg/'99-test.conf').write_text('wireplumber.profiles = { main = { monitor.alsa = disabled monitor.bluez = disabled monitor.v4l2 = disabled monitor.libcamera = disabled } }')
    harness.spawn(['wireplumber'],env,'target-wireplumber')
    lock=run/'lock.json';lock.write_text('{}')
    harness.spawn(['/usr/bin/python3',str(ROOT/'tests/native_screen_saver.py'),str(lock)],env,'target-saver')
    lib=prefix/'lib/x86_64-linux-gnu'
    native=dict(env,LD_LIBRARY_PATH=f'{lib}:{lib}/mutter-18',MUTTER_NATIVE_TAKEOVER='1',MUTTER_NATIVE_TAKEOVER_TEST='1')
    for name in ('MUTTER_NATIVE_GREETER','MUTTER_NATIVE_BOOT_PRIVACY'):
        native.pop(name,None)
    harness.spawn([str(prefix/'bin/mutter'),'--headless','--no-x11','--wayland',
        '--wayland-display=handover-target','--virtual-monitor=1280x720',
        '--mutter-plugin='+str(lib/'mutter-18/plugins/libdefault.so')],native,'target-mutter')
    harness.wait_for(lambda:(run/'handover-target').exists(),'Target compositor missing')
    observer=Gio.DBusConnection.new_for_address_sync(env['DBUS_SESSION_BUS_ADDRESS'],
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,None,None)
    def state():
        try:
            return observer.call_sync('org.gnome.Mutter.NativeTakeover','/org/gnome/Mutter/NativeTakeover',
                'org.gnome.Mutter.NativeTakeover','GetState',None,None,0,1000,None).unpack()[0]
        except Exception:return None
    harness.wait_for(lambda:state()=='local','Target native API missing')
    app_state=run/'app.json';app_state.unlink(missing_ok=True)
    harness.spawn(['/usr/bin/python3',str(ROOT/'tests/desktop_app.py')],
        dict(env,WAYLAND_DISPLAY='handover-target',GDK_BACKEND='wayland',TAKEOVER_TEST_APP_STATE=str(app_state)), 'target-app')
    harness.wait_for(app_state.exists,'Target app missing')
    gate=run/'gate.sock';gate.unlink(missing_ok=True)
    harness.spawn(['/usr/bin/python3','-m','takeover.native_broker','--socket',str(gate),
        '--session','/org/freedesktop/login1/session/test'],env,'target-broker',cwd=runtime_root)
    harness.wait_for(gate.exists,'Target broker missing')
    grd_env=dict(env,GSETTINGS_BACKEND='keyfile',GRD_TAKEOVER_SOCKET=str(gate),GRD_NATIVE_TAKEOVER='1',GRD_NATIVE_LOOPBACK='1',
        GNOME_REMOTE_DESKTOP_TEST_RDP_USERNAME='native-test',GNOME_REMOTE_DESKTOP_TEST_RDP_PASSWORD=password,
        LD_LIBRARY_PATH=str(ROOT/'vendor/grd-deps/root/usr/lib/x86_64-linux-gnu' if runtime_root==ROOT else runtime_root/'build/grd-prefix/lib/x86_64-linux-gnu'))
    for schema,name,value in [('org.gnome.desktop.remote-desktop.rdp','tls-cert',str(cert)),
        ('org.gnome.desktop.remote-desktop.rdp','tls-key',str(key)),
        ('org.gnome.desktop.remote-desktop.rdp.headless','enable','true')]:
        subprocess.run(['gsettings','set',schema,name,value],env=grd_env,check=True)
    grd=harness.spawn([str(runtime_root/'build/grd-prefix/libexec/gnome-remote-desktop-daemon'),
                   '--headless','--rdp-port=43393'],grd_env,'target-grd')
    harness.wait_for(lambda:'RDP server started' in (harness.ART/'target-grd.log').read_text(),'Target RDP listener missing')
    state.grd=grd
    return state,app_state
