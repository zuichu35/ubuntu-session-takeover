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
        raise RuntimeError('无法识别系统 GNOME Shell 启动命令，未修改配置。')
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
            '[Desktop Entry]\nType=Application\nName=远程桌面设置\n'
            'Comment=调整远程接管桌面的缩放比例\n'
            'Exec=/usr/bin/python3 ' + str(ROOT / 'scripts/remote-settings.py') + '\n'
            'Icon=preferences-desktop-display\nTerminal=false\nCategories=Settings;DesktopSettings;\n',
        Path.home() / '.config/autostart/ubuntu-native-takeover.desktop':
            '[Desktop Entry]\nType=Application\nName=原生桌面远程接管\n'
            'Exec=/usr/bin/systemctl --user start ubuntu-native-takeover.service\n'
            'OnlyShowIn=GNOME;\nX-GNOME-Autostart-enabled=true\n',
    }


def setup(args):
    if os.getuid() == 0:
        raise RuntimeError('请以桌面用户运行 setup，不要使用 sudo。')
    report = check()
    if not report['compatible']:
        raise RuntimeError('安装包未通过当前系统的兼容性验证：' + json.dumps(report, ensure_ascii=False))
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
        raise RuntimeError('已有原生接管配置。请先使用原版本的回退入口，避免覆盖：' + ', '.join(conflicts))
    username = args.username or input('RDP 用户名：').strip()
    if not username or '\n' in username:
        raise RuntimeError('用户名不能为空。')
    password = getpass.getpass('设置 RDP 密码：')
    if not password or password != getpass.getpass('再次输入密码：'):
        raise RuntimeError('两次密码不一致或密码为空，未修改配置。')
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
        raise RuntimeError('证书或私钥缺失，未覆盖现有证书文件。')
    result = subprocess.run([str(ROOT / 'build/grd-prefix/bin/grdctl'), '--headless',
                             'rdp', 'set-credentials', username], input=password + '\n',
                            env=env, text=True, capture_output=True)
    del password
    if result.returncode:
        raise RuntimeError('保存 RDP 凭据失败，未启用服务。')
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
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    print('已配置。保存工作后注销并重新登录 Ubuntu，再连接 本机IP:3392。当前桌面未重启。')


def check_removal():
    if LOGIN_CONFIG.exists():
        raise RuntimeError('请先关闭系统登录入口：sudo python3 /opt/ubuntu-native-takeover/scripts/disable-login.py')
    blocked = []
    for account in pwd.getpwall():
        p = Path(account.pw_dir) / '.config/systemd/user/org.gnome.Shell@ubuntu.service.d/90-native-takeover.conf'
        if p.is_file() and '/opt/ubuntu-native-takeover/' in p.read_text():
            blocked.append(account.pw_name)
    if blocked:
        raise RuntimeError('以下用户仍启用了接管，请先以各用户运行 ubuntu-native-takeover disable：' + ', '.join(blocked))


def main():
    parser = argparse.ArgumentParser(description='Ubuntu 原生会话远程接管（实验版）')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('check', help='校验版本、安装文件和动态库')
    setup_parser = sub.add_parser('setup', help='交互配置 RDP 凭据，下次登录启用')
    setup_parser.add_argument('--username')
    setup_parser.add_argument('--dry-run', action='store_true')
    sub.add_parser('status')
    sub.add_parser('disable', help='回退启动入口，不注销当前桌面')
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
            raise RuntimeError('请以原桌面用户运行 disable，不要使用 sudo。')
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
