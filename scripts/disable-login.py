#!/usr/bin/python3
"""Remove the system login gateway; preserve account data and disabled autologin."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess

if os.geteuid()!=0:
    raise SystemExit('Run with sudo')
manifest=Path('/var/lib/ubuntu-native-login/install.json')
saved=json.loads(manifest.read_text())
for path,expected in saved['installed_hashes'].items():
    p=Path(path)
    if p.exists() and hashlib.sha256(p.read_bytes()).hexdigest()!=expected:
        raise SystemExit('Configuration changed; review before rollback: '+path)
subprocess.run(['systemctl','disable','--now','ubuntu-native-login.service'],check=True)
for path,previous in saved['files'].items():
    p=Path(path)
    if previous is None: p.unlink(missing_ok=True)
    else:p.write_text(previous)
subprocess.run(['systemctl','daemon-reload'],check=True)
for name in ('gdm',saved['user']):
    account=pwd.getpwnam(name)
    if not Path(f'/run/user/{account.pw_uid}/bus').exists():continue
    command=['runuser','-u',name,'--','env',f'XDG_RUNTIME_DIR=/run/user/{account.pw_uid}',
             f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{account.pw_uid}/bus','systemctl','--user']
    subprocess.run(command+['daemon-reload'],check=True)
    if name=='gdm':subprocess.run(command+['stop','ubuntu-native-greeter.service'],check=False)
    else:subprocess.run(command+['restart','ubuntu-native-takeover.service'],check=False)
manifest.rename(manifest.with_name('install.disabled.json'))
print('System login gateway removed. Autologin remains disabled. Current desktop was not logged out.')
