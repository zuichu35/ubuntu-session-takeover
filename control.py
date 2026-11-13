#!/usr/bin/env python3
"""Per-user activation for the packaged native compositor. No automatic logout."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import pwd
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from takeover.compatibility import inspect_candidate
LOGIN_CONFIG = Path('/etc/ubuntu-native-login/config.json')
CONFIG = Path.home() / '.config/ubuntu-native-takeover'
DATA = Path.home() / '.local/share/ubuntu-native-takeover'
UNITS = Path.home() / '.config/systemd/user'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check():
    candidate = inspect_candidate(ROOT, required_owner=0)
    files = json.loads((ROOT / 'package-files.json').read_text())
    damaged = [p for p, expected in files.items() if not (ROOT / p).exists() or sha(ROOT / p) != expected]
    return {'compatible': not damaged and candidate.get('status') == 'verified'
                          and candidate.get('production_eligible') is True,
            'startup_policy': 'verified-private-or-guarded-fallback',
            'candidate': candidate, 'damaged_package_files': damaged, 'port': 3392,
            'physical_hardware_acceptance': 'pending', 'windows_mstsc_acceptance': 'pending'}


def launch_files():
    unit = Path('/usr/lib/systemd/user/org.gnome.Shell@.service').read_text()
    commands = [l.split('=', 1)[1] for l in unit.splitlines() if l.startswith('ExecStart=')]
    if len(commands) != 1 or '/usr/bin/gnome-shell' not in shlex.split(commands[0]):
        raise RuntimeError('Could not identify the system GNOME Shell launch command; configuration unchanged.')
    # Use the same unit name as the development version so conflicting installs
    # are detected and cannot silently replace one another.
    return {
        UNITS / 'org.gnome.Shell@ubuntu.service.d/90-native-takeover.conf':
            '[Service]\nExecStart=\nExecStart=/usr/bin/python3 ' + str(ROOT / 'scripts/native-shell.py') + ' -- ' + commands[0] + '\n',
        UNITS / 'org.gnome.Shell-disable-extensions.service.d/90-native-takeover.conf':
            '[Service]\nExecCondition=/usr/bin/false\n',
        UNITS / 'ubuntu-native-takeover.service':
            '[Unit]\nDescription=Native GNOME RDP takeover\nPartOf=graphical-session.target\n'
            'After=graphical-session.target\n\n[Service]\nType=simple\n'
            'ExecStart=/usr/bin/python3 ' + str(ROOT / 'scripts/native-session.py') + '\n'
            'KillMode=control-group\nTimeoutStopSec=20\n',
        Path.home() / '.local/share/applications/ubuntu-native-takeover-settings.desktop':
            '[Desktop Entry]\nType=Application\nName=UST\nGenericName=Ubuntu Session Takeover\n'
            'Comment=Live state, scale and monitors of the remotely taken-over desktop\n'
            'Exec=/usr/bin/python3 ' + str(ROOT / 'scripts/remote-settings.py') + '\n'
            'Icon=preferences-desktop-display\nTerminal=false\nCategories=Settings;DesktopSettings;\n'
            'Keywords=ust;takeover;remote;rdp;scale;monitor;\n',
        Path.home() / '.local/bin/ust':
            '#!/bin/sh\nexec /usr/bin/python3 ' + str(ROOT / 'scripts/remote-settings.py') + ' "$@"\n',
        Path.home() / '.config/autostart/ubuntu-native-takeover.desktop':
            '[Desktop Entry]\nType=Application\nName=Native Desktop Remote Takeover\n'
            'Exec=/usr/bin/systemctl --user start ubuntu-native-takeover.service\n'
            'OnlyShowIn=GNOME;\nX-GNOME-Autostart-enabled=true\n',
    }


def setup(args):
    if os.getuid() == 0:
        raise RuntimeError('Run setup as the desktop user; do not use sudo.')
    report = check()
    if not report['compatible']:
        raise RuntimeError('The package did not pass the compatibility check for this system: ' + json.dumps(report, ensure_ascii=False))
    files = launch_files()
    conflicts = [str(p) for p in files if p.exists()]
    if (CONFIG / 'install.json').exists():
        conflicts.append(str(CONFIG / 'install.json'))
    if args.dry_run:
        print(json.dumps({'would_write': [str(p) for p in files], 'existing_configuration': conflicts,
                          'would_generate_tls': True, 'credentials': 'prompted at actual setup',
                          'restart_desktop': False}, ensure_ascii=False, indent=2))
        return
    if conflicts:
        raise RuntimeError('Native takeover configuration already exists. Use the previous version’s rollback entry first to avoid overwriting it: ' + ', '.join(conflicts))
    username = args.username or input('RDP username: ').strip()
    if not username or '\n' in username:
        raise RuntimeError('Username must not be empty.')
    password = getpass.getpass('Set RDP password: ')
    if not password or password != getpass.getpass('Re-enter password: '):
        raise RuntimeError('Passwords do not match or are empty; configuration unchanged.')
    os.umask(0o077)
    for directory in (CONFIG, DATA, DATA / 'rdp'):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile = CONFIG / 'dconf-profile'
    profile.write_text('user-db:ubuntu_native_takeover\n')
    env = dict(os.environ, DCONF_PROFILE=str(profile), XDG_DATA_HOME=str(DATA / 'rdp'))
    for key in ('GSETTINGS_BACKEND', 'GSETTINGS_SCHEMA_DIR'):
        env.pop(key, None)
    cert, key = DATA / 'tls.crt', DATA / 'tls.key'
    if not cert.exists() and not key.exists():
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-days', '825',
                        '-subj', '/CN=Ubuntu Native Takeover', '-keyout', str(key), '-out', str(cert)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        key.chmod(0o600)
    elif not cert.exists() or not key.exists():
        raise RuntimeError('Certificate or private key is missing; existing certificate files were not overwritten.')
    result = subprocess.run([str(ROOT / 'build/grd-prefix/bin/grdctl'), '--headless',
                             'rdp', 'set-credentials', username], input=password + '\n',
                            env=env, text=True, capture_output=True)
    del password
    if result.returncode:
        raise RuntimeError('Failed to save RDP credentials; service not enabled.')
    for schema, name, value in [('org.gnome.desktop.remote-desktop.rdp', 'tls-cert', str(cert)),
                               ('org.gnome.desktop.remote-desktop.rdp', 'tls-key', str(key)),
                               ('org.gnome.desktop.remote-desktop.rdp', 'view-only', 'false'),
                               ('org.gnome.desktop.remote-desktop.rdp.headless', 'enable', 'true'),
                               ('org.gnome.desktop.remote-desktop.rdp.headless', 'port', '3392'),
                               ('org.gnome.desktop.remote-desktop.rdp.headless', 'negotiate-port', 'false')]:
        subprocess.run(['gsettings', 'set', schema, name, value], env=env, check=True)
    manifest = {'files': {str(p): None for p in files}, 'port': 3392,
                'installed_files': {str(p): hashlib.sha256(t.encode()).hexdigest() for p, t in files.items()},
                'legacy_autostart': None, 'legacy_enabled_units': []}
    (CONFIG / 'install.json').write_text(json.dumps(manifest, indent=2) + '\n')
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if text.startswith('#!'):
            path.chmod(0o755)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    print('Configured. Save your work, log out and log back in to Ubuntu, then connect to <this-machine-IP>:3392. The current desktop was not restarted.')


def check_removal():
    if LOGIN_CONFIG.exists():
        raise RuntimeError('Disable the system login entry first: sudo python3 /opt/ubuntu-native-takeover/scripts/disable-login.py')
    blocked = []
    for account in pwd.getpwall():
        p = Path(account.pw_dir) / '.config/systemd/user/org.gnome.Shell@ubuntu.service.d/90-native-takeover.conf'
        if p.is_file() and '/opt/ubuntu-native-takeover/' in p.read_text():
            blocked.append(account.pw_name)
    if blocked:
        raise RuntimeError('These users still have takeover enabled; run ubuntu-native-takeover disable as each of them first: ' + ', '.join(blocked))


def main():
    parser = argparse.ArgumentParser(description='Remote takeover of the native Ubuntu session (experimental)')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('check', help='Verify version, installed files and shared libraries')
    setup_parser = sub.add_parser('setup', help='Interactively configure RDP credentials; enabled at next login')
    setup_parser.add_argument('--username')
    setup_parser.add_argument('--dry-run', action='store_true')
    sub.add_parser('status')
    sub.add_parser('disable', help='Roll back the startup entry without logging out the current desktop')
    sub.add_parser('check-removal')
    args = parser.parse_args()
    if args.command == 'check':
        result = check()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['compatible'] else 1
    if args.command == 'setup':
        setup(args)
    elif args.command == 'disable':
        if os.getuid() == 0:
            raise RuntimeError('Run disable as the original desktop user; do not use sudo.')
        subprocess.run(['/usr/bin/python3', str(ROOT / 'scripts/disable-native.py')], check=True)
    elif args.command == 'status':
        subprocess.run(['systemctl', '--user', 'status', '--no-pager', 'ubuntu-native-takeover.service'], check=False)
        subprocess.run(['gdbus', 'call', '--session', '--dest', 'org.gnome.Mutter.NativeTakeover',
                        '--object-path', '/org/gnome/Mutter/NativeTakeover',
                        '--method', 'org.gnome.Mutter.NativeTakeover.GetState'], check=False)
    elif args.command == 'check-removal':
        check_removal()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError, EOFError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
