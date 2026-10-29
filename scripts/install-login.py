#!/usr/bin/python3
"""Install native GDM entrypoint for the next login, preserving the live session."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess

PREFIX = Path('/opt/ubuntu-native-takeover')
STATE = Path('/var/lib/ubuntu-native-login')


def main():
    raise RuntimeError('This historical installer does not support the current dynamic GDM lifecycle. '
                       'It is retained as source reference only; see docs/DEPLOYMENT.md.')
    parser = argparse.ArgumentParser()
    parser.add_argument('--user', required=True)
    parser.add_argument('--package', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run the installer with sudo; it does not log out or restart GDM.')
    user, greeter = pwd.getpwnam(args.user), pwd.getpwnam('gdm')
    if user.pw_uid < 1000:
        raise SystemExit('A regular desktop account is required')
    subprocess.run(['dpkg', '-i', str(args.package.resolve())], check=True)
    subprocess.run(['/usr/bin/python3', str(PREFIX/'control.py'), 'check'], check=True)
    home, gdm_home = Path(user.pw_dir), Path(greeter.pw_dir)
    state = {'files':{},'user':args.user}
    STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    manifest = STATE/'install.json'
    if manifest.exists():
        raise RuntimeError('Native login configuration already exists; review before replacing')
    def write(path, text, owner=None):
        path=Path(path)
        state['files'][str(path)] = path.read_text() if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o644)
        if owner: os.chown(path,owner.pw_uid,owner.pw_gid)
    config = gdm_home/'.config/ubuntu-native-takeover'
    data = gdm_home/'.local/share/ubuntu-native-takeover'
    for path in (config,data,data/'rdp',data/'rdp/gnome-remote-desktop'):
        path.mkdir(parents=True,mode=0o700,exist_ok=True)
        os.chown(path,greeter.pw_uid,greeter.pw_gid)
    for relative in ('tls.crt','tls.key','rdp/gnome-remote-desktop/credentials.ini'):
        source=home/'.local/share/ubuntu-native-takeover'/relative
        target=data/relative
        if target.exists(): raise RuntimeError('Refusing to overwrite greeter credentials: '+str(target))
        shutil.copyfile(source,target);target.chmod(0o600);os.chown(target,greeter.pw_uid,greeter.pw_gid)
        state.setdefault('created_credentials',[]).append(str(target))
    write(config/'dconf-profile','user-db:ubuntu_native_takeover\n',greeter)
    settings_code = """from gi.repository import Gio, GLib
import sys
rdp=Gio.Settings.new('org.gnome.desktop.remote-desktop.rdp')
rdp.set_string('tls-cert',sys.argv[1]+'/tls.crt');rdp.set_string('tls-key',sys.argv[1]+'/tls.key')
rdp.set_boolean('view-only',False)
headless=Gio.Settings.new('org.gnome.desktop.remote-desktop.rdp.headless')
headless.set_boolean('enable',True);headless.set_value('port',GLib.Variant('q',3394));headless.set_boolean('negotiate-port',False)
Gio.Settings.sync()
"""
    subprocess.run(['runuser','-u','gdm','--','env','DCONF_PROFILE='+str(config/'dconf-profile'),
                    'dbus-run-session','--','/usr/bin/python3','-c',settings_code,str(data)],check=True)
    write('/etc/ubuntu-native-login/config.json',json.dumps({'user':args.user})+'\n')
    write('/etc/systemd/system/ubuntu-native-login.service',f'''[Unit]
Description=Authenticated RDP entry to the native GDM and desktop sessions
After=systemd-logind.service dbus.service
Before=display-manager.service
Requires=systemd-logind.service dbus.service
[Service]
Type=simple
WorkingDirectory={PREFIX}
ExecStart=/usr/bin/python3 -m takeover.login_gateway
Restart=on-failure
RestartSec=3
RuntimeDirectory=ubuntu-native-login
RuntimeDirectoryMode=0755
RuntimeDirectoryPreserve=yes
UMask=0022
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/run/ubuntu-native-login
PrivateTmp=yes
[Install]
WantedBy=multi-user.target
''')
    write('/etc/systemd/user/org.gnome.Shell@.service.d/80-native-login.conf',f'''[Service]
ExecStart=
ExecStart=/usr/bin/python3 {PREFIX}/scripts/native-shell.py -- /usr/bin/gnome-shell --mode=%i
ExecStartPost=/usr/bin/python3 {PREFIX}/scripts/finish-login-guard.py
''')
    write('/etc/systemd/user/org.gnome.Shell@gdm.service.d/95-native-login.conf','''[Service]
ExecStartPost=/usr/bin/systemctl --user --no-block start ubuntu-native-greeter.service
''')
    write('/etc/systemd/user/ubuntu-native-greeter.service',f'''[Unit]
Description=RDP capture of the actual native GDM greeter
ConditionUser=gdm
After=org.gnome.Shell@gdm.service
PartOf=gnome-session@gnome-login.target
[Service]
Type=simple
ExecStart=/usr/bin/python3 {PREFIX}/scripts/native-session.py --greeter
Restart=on-failure
RestartSec=3
KillMode=control-group
TimeoutStopSec=20
''')
    write(home/'.config/systemd/user/org.gnome.Shell@ubuntu.service.d/90-native-takeover.conf',f'''[Service]
ExecStart=
ExecStart=/usr/bin/python3 {PREFIX}/scripts/native-shell.py -- /usr/bin/gnome-shell --mode=%i
''',user)
    write(home/'.config/systemd/user/ubuntu-native-takeover.service.d/95-system-login.conf',f'''[Service]
ExecStart=
ExecStart=/usr/bin/python3 {PREFIX}/scripts/native-session.py
Restart=on-failure
RestartSec=3
''',user)
    gdm=Path('/etc/gdm3/custom.conf')
    original=gdm.read_text()
    changed=re.sub(r'(?m)^(\s*(?:AutomaticLoginEnable|TimedLoginEnable)\s*=).*$',r'\1false',original)
    write(gdm,changed)
    state['installed_hashes']={name:hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in state['files']}
    manifest.write_text(json.dumps(state,indent=2)+'\n');manifest.chmod(0o600)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','ubuntu-native-login.service'],check=True)
    subprocess.run(['runuser','-u',args.user,'--','env','XDG_RUNTIME_DIR=/run/user/'+str(user.pw_uid),
                    'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/'+str(user.pw_uid)+'/bus',
                    'systemctl','--user','daemon-reload'],check=True)
    print('Native login installed for next login/boot. Current desktop and GDM were not restarted.')


if __name__ == '__main__':
    main()
