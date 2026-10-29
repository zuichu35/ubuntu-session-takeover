"""Paths shared by the installed user services and local entry point."""
import json
import os
from pathlib import Path
import subprocess
from functools import lru_cache

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path.home() / '.config/ubuntu-takeover'
DATA = Path.home() / '.local/share/ubuntu-takeover'
RUNTIME = Path(f'/run/user/{os.getuid()}') / 'ubuntu-takeover'
BUS = f'unix:path={RUNTIME}/workspace-bus'


@lru_cache(maxsize=1)
def manager_session():
    sessions = json.loads(subprocess.check_output(['loginctl', 'list-sessions', '--json=short']))
    for s in sessions:
        if s.get('uid') == os.getuid() and s.get('class') == 'manager':
            return str(s['session'])
    raise RuntimeError('logind user-manager session missing')


def private_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def workspace_env():
    env = dict(os.environ)
    for key in ('DISPLAY', 'XAUTHORITY', 'XDG_SESSION_ID', 'SESSION_MANAGER',
                'GNOME_DESKTOP_SESSION_ID', 'GSETTINGS_BACKEND', 'GSETTINGS_SCHEMA_DIR',
                'DBUS_STARTER_ADDRESS', 'DBUS_STARTER_BUS_TYPE', 'NOTIFY_SOCKET'):
        env.pop(key, None)
    env.update(DBUS_SESSION_BUS_ADDRESS=BUS, WAYLAND_DISPLAY='wayland-takeover',
               XDG_SESSION_ID=manager_session(),
               XDG_RUNTIME_DIR=str(RUNTIME / 'desktop'),
               PIPEWIRE_RUNTIME_DIR=f'/run/user/{os.getuid()}',
               PULSE_SERVER=f'unix:/run/user/{os.getuid()}/pulse/native',
               DCONF_PROFILE=str(CONFIG / 'dconf-profile'), XDG_SESSION_TYPE='wayland',
               XDG_CURRENT_DESKTOP='GNOME', XDG_SESSION_DESKTOP='gnome',
               GTK_IM_MODULE='fcitx', QT_IM_MODULE='fcitx', XMODIFIERS='@im=fcitx',
               GNOME_SHELL_SESSION_MODE='user')
    activation = RUNTIME / 'activation-env.json'
    if activation.exists():
        env.update(json.loads(activation.read_text()))
    return env


def grd_env():
    return dict(workspace_env(), XDG_DATA_HOME=str(DATA / 'rdp'),
                GRD_TAKEOVER_SOCKET=str(RUNTIME / 'gate.sock'),
                LD_LIBRARY_PATH=str(ROOT / 'vendor/grd-deps/root/usr/lib/x86_64-linux-gnu'))
