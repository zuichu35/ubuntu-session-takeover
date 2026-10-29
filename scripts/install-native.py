#!/usr/bin/env python3
"""Prepare next login to use the native patch; never restart the current shell."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from takeover.compatibility import inspect_candidate
CONFIG = Path.home() / '.config/ubuntu-native-takeover'
DATA = Path.home() / '.local/share/ubuntu-native-takeover'
USER = Path.home() / '.config/systemd/user'


def main():
    candidate = inspect_candidate(ROOT, required_owner=0)
    if candidate.get('status') != 'verified' or candidate.get('production_eligible') is not True:
        raise RuntimeError('Activation requires a root-managed, validated runtime: ' + json.dumps(candidate))
    os.umask(0o077)
    result = json.loads((ROOT / 'artifacts/native-integration-result.json').read_text())
    if not all(result.get(k) for k in ('rdp_input_to_original_app', 'black_stage_with_live_remote_scene',
                                      'broker_crash_recovers_lock', 'failed_lock_stays_black')):
        raise RuntimeError('Native integration verification has not passed')
    shell_result = json.loads((ROOT / 'artifacts/native-shell-integration-result.json').read_text())
    if not shell_result.get('rdp_input_to_original_app') or not shell_result.get('broker_crash_recovers_lock'):
        raise RuntimeError('Real GNOME Shell integration verification has not passed')
    for evidence in (result, shell_result):
        if not evidence.get('tested_binaries') or any(hashlib.sha256(Path(p).read_bytes()).hexdigest() != h
                                                    for p, h in evidence['tested_binaries'].items()):
            raise RuntimeError('Test evidence does not match the current binaries')
    abi = ['/usr/lib/x86_64-linux-gnu/libmutter-18.so.0', '/usr/bin/gnome-shell',
           '/usr/lib/gnome-shell/libshell-18.so']
    abi += [str(p) for p in Path('/usr/lib/x86_64-linux-gnu/mutter-18').glob('*.typelib')]
    (ROOT / 'build/native-system-abi.json').write_text(json.dumps(
        {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in abi}, indent=2) + '\n')
    unit = Path('/usr/lib/systemd/user/org.gnome.Shell@.service').read_text()
    starts = [l.removeprefix('ExecStart=') for l in unit.splitlines() if l.startswith('ExecStart=')]
    if len(starts) != 1 or '/usr/bin/gnome-shell' not in shlex.split(starts[0]):
        raise RuntimeError('Unrecognized system GNOME Shell launch command')
    for p in (CONFIG, DATA, DATA / 'rdp/gnome-remote-desktop'):
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
    credentials = DATA / 'rdp/gnome-remote-desktop/credentials.ini'
    if not credentials.exists():
        source = Path.home() / '.local/share/ubuntu-takeover/rdp/gnome-remote-desktop/credentials.ini'
        if not source.exists():
            raise RuntimeError('Existing authorized RDP credential store was not found')
        shutil.copyfile(source, credentials)
        credentials.chmod(0o600)
    for name in ('tls.crt', 'tls.key'):
        if not (DATA / name).exists():
            shutil.copyfile(Path.home() / '.local/share/ubuntu-takeover' / name, DATA / name)
            (DATA / name).chmod(0o600)
    profile = CONFIG / 'dconf-profile'
    profile.write_text('user-db:ubuntu_native_takeover\n')
    env = dict(os.environ, DCONF_PROFILE=str(profile))
    for schema, key, value in [('org.gnome.desktop.remote-desktop.rdp', 'tls-cert', str(DATA / 'tls.crt')),
                               ('org.gnome.desktop.remote-desktop.rdp', 'tls-key', str(DATA / 'tls.key')),
                               ('org.gnome.desktop.remote-desktop.rdp', 'view-only', 'false'),
                               ('org.gnome.desktop.remote-desktop.rdp.headless', 'enable', 'true'),
                               ('org.gnome.desktop.remote-desktop.rdp.headless', 'port', '3392'),
                               ('org.gnome.desktop.remote-desktop.rdp.headless', 'negotiate-port', 'false')]:
        subprocess.run(['gsettings', 'set', schema, key, value], env=env, check=True)
    files = {
        USER / 'org.gnome.Shell@ubuntu.service.d/90-native-takeover.conf':
            '[Service]\nExecStart=\nExecStart=/usr/bin/python3 ' + str(ROOT / 'scripts/native-shell.py') + ' -- ' + starts[0] + '\n',
        USER / 'org.gnome.Shell-disable-extensions.service.d/90-native-takeover.conf':
            # Dependency lists cannot be cleared via systemd drop-ins. Skip
            # only the settings mutation; leave session shutdown handling intact.
            '[Service]\nExecCondition=/usr/bin/false\n',
        USER / 'ubuntu-native-takeover.service':
            '[Unit]\nDescription=RDP takeover of the original physical GNOME desktop\n'
            'PartOf=graphical-session.target\nAfter=graphical-session.target\n\n'
            '[Service]\nType=simple\nExecStart=/usr/bin/python3 ' + str(ROOT / 'scripts/native-session.py') + '\n'
            'KillMode=control-group\nTimeoutStopSec=20\n',
        Path.home() / '.local/share/applications/ubuntu-native-takeover-settings.desktop':
            '[Desktop Entry]\nType=Application\nName=远程桌面设置\n'
            'Comment=调整远程接管桌面的缩放比例\n'
            'Exec=/usr/bin/python3 ' + str(ROOT / 'scripts/remote-settings.py') + '\n'
            'Icon=preferences-desktop-display\nTerminal=false\nCategories=Settings;DesktopSettings;\n',
        Path.home() / '.config/autostart/ubuntu-native-takeover.desktop':
            '[Desktop Entry]\nType=Application\nName=原生桌面远程接管\n'
            'Exec=/usr/bin/systemctl --user start ubuntu-native-takeover.service\n'
            'OnlyShowIn=GNOME;\nX-GNOME-Autostart-enabled=true\n',
    }
    old = {}
    for target, text in files.items():
        old[str(target)] = target.read_text() if target.exists() else None
        if target.exists() and target.read_text() != text:
            raise RuntimeError('Refusing to overwrite existing deployment file: ' + str(target))
    for target, text in files.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    manifest = CONFIG / 'install.json'
    if not manifest.exists():
        legacy = Path.home() / '.config/autostart/ubuntu-takeover.desktop'
        legacy_text = legacy.read_text() if legacy.exists() else None
        enabled = []
        for name in ('ubuntu-takeover-broker.service', 'ubuntu-takeover-rdp.service', 'ubuntu-takeover-workspace.service'):
            p = subprocess.run(['systemctl', '--user', 'is-enabled', name], capture_output=True, text=True)
            if p.stdout.strip() == 'enabled':
                enabled.append(name)
        manifest.write_text(json.dumps({'files': old, 'port': 3392, 'active_after_next_login': True,
                                        'installed_files': {str(p): hashlib.sha256(t.encode()).hexdigest() for p, t in files.items()},
                                        'legacy_autostart': legacy_text, 'legacy_enabled_units': enabled}, indent=2) + '\n')
        if legacy_text is not None:
            # Suppress the old viewer at future logins; keep current work processes.
            legacy.write_text(legacy_text.rstrip() + '\nHidden=true\n')
        if enabled:
            subprocess.run(['systemctl', '--user', 'disable', *enabled], check=True)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    print('Native takeover installed for the next Ubuntu login. Current compositor was not restarted. Port: 3392')


if __name__ == '__main__':
    main()
