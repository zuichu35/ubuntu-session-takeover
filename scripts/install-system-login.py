#!/usr/bin/python3
"""First-time install of the 3392 system login entry for an existing package.

The maintenance chain (install-greeter-fix.py and its successors) upgrades a
historical login install and cannot bootstrap a machine that only ran the
per-user ``control.py setup``. This installer writes the same final state
directly for the installed /opt/ubuntu-native-takeover prefix:

- root gateway ubuntu-native-login.service (public 3392, loopback relay);
- root-owned, read-only GRD settings and credentials for dynamic GDM greeters;
- system-wide Shell launcher, startup-guard and greeter-capture user units;
- the desktop user's backend override (restart and watchdog).

``plan`` needs no root. ``install`` refuses to replace any existing file,
records a manifest in the disable-login.py format and enables the gateway; it
never restarts GDM or a desktop. ``rollback`` removes exactly what was created.
"""
import argparse
import contextlib
import grp
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import subprocess
import sys
import tempfile


PREFIX = '/opt/ubuntu-native-takeover'
STATE = '/var/lib/ubuntu-native-login'
MANIFEST = STATE + '/install.json'
LOGIN_CONFIG = '/etc/ubuntu-native-login/config.json'
GREETER_CONFIG = '/etc/ubuntu-native-login/greeter'
GREETER_DATA = '/var/lib/ubuntu-native-greeter'
GATEWAY = '/etc/systemd/system/ubuntu-native-login.service'
SHELL_DROPIN = '/etc/systemd/user/org.gnome.Shell@.service.d/80-native-login.conf'
GDM_SHELL_DROPIN = '/etc/systemd/user/org.gnome.Shell@gdm.service.d/95-native-login.conf'
GREETER_UNIT = '/etc/systemd/user/ubuntu-native-greeter.service'
GUARD_UNIT = '/etc/systemd/user/ubuntu-native-login-guard.service'
SECRETS = ('tls.crt', 'tls.key', 'rdp/gnome-remote-desktop/credentials.ini')
GDM_CONFIG = '/etc/gdm3/custom.conf'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def gateway_unit(prefix=PREFIX):
    return f'''[Unit]
Description=Authenticated RDP entry to the native GDM and desktop sessions
After=systemd-logind.service dbus.service
Before=display-manager.service
Requires=systemd-logind.service dbus.service
[Service]
Type=simple
WorkingDirectory={prefix}
ExecStart=/usr/bin/python3 -m takeover.login_gateway
Restart=on-failure
RestartSec=3
TimeoutStopSec=10s
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
'''


def greeter_unit(prefix=PREFIX):
    return f'''[Unit]
Description=RDP capture of the actual native GDM greeter
ConditionGroup=gdm
After=org.gnome.Shell@gdm.service
PartOf=gnome-session@gnome-login.target
[Service]
Type=simple
ExecStart=/usr/bin/python3 {prefix}/scripts/native-session.py --greeter
Restart=on-failure
RestartSec=3
WatchdogSec=60
NotifyAccess=main
KillMode=control-group
TimeoutStopSec=20
'''


def backend_override(prefix=PREFIX):
    return f'''[Service]
ExecStart=
ExecStart=/usr/bin/python3 {prefix}/scripts/native-session.py
Restart=on-failure
RestartSec=3
WatchdogSec=60
NotifyAccess=main
'''


def load_templates(prefix):
    """Use the installed, root-owned modules: the same code the runtime reads."""
    namespace = {}
    for name in ('takeover/startup_guard.py', 'takeover/greeter_config.py'):
        source = Path(prefix) / name
        module = {'__name__': 'installed_' + source.stem, '__file__': str(source)}
        exec(compile(source.read_text(), str(source), 'exec'), module)
        namespace[source.stem] = module
    return namespace


class Installer:
    def __init__(self, user, *, root=Path('/'), prefix=PREFIX, runner=subprocess.run,
                 account_lookup=pwd.getpwnam, gdm_gid=None, owner_uid=0, owner_gid=0):
        self.root = Path(root)
        self.prefix = prefix
        self.runner = runner
        self.account = account_lookup(user)
        if self.account.pw_uid < 1000:
            raise RuntimeError('The desktop account must be a regular user (UID >= 1000)')
        self.home = self.account.pw_dir
        self.gdm_gid = grp.getgrnam('gdm').gr_gid if gdm_gid is None else gdm_gid
        self.owner_uid, self.owner_gid = owner_uid, owner_gid

    # -- filesystem helpers -------------------------------------------------
    def path(self, absolute):
        value = PurePosixPath(absolute)
        if not value.is_absolute() or '..' in value.parts:
            raise RuntimeError('Expected an absolute destination path: ' + absolute)
        return self.root / str(value).lstrip('/')

    def run(self, args, **kwargs):
        result = self.runner([str(a) for a in args], check=False, capture_output=True,
                             text=True, timeout=60, **kwargs)
        if result.returncode:
            raise RuntimeError(f'{args[0]} failed ({result.returncode}): {(result.stderr or "")[-2000:]}')
        return result.stdout

    def user_command(self, args):
        uid = self.account.pw_uid
        return ['runuser', '-u', self.account.pw_name, '--', 'env', f'XDG_RUNTIME_DIR=/run/user/{uid}',
                f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus', *args]

    def no_symlinks(self, path):
        for part in (path, *path.parents):
            if part.is_symlink():
                raise RuntimeError('Refusing a symlink in a destination path: ' + str(part))
            if part == self.root:
                break

    def make_directories(self, absolute, mode, uid, gid, created):
        path = self.path(absolute)
        self.no_symlinks(path)
        missing = []
        while not path.exists():
            missing.append(path)
            path = path.parent
        for directory in reversed(missing):
            directory.mkdir(mode=mode)
            os.chown(directory, uid, gid)
            directory.chmod(mode)
            created.append('/' + str(directory.relative_to(self.root)))

    def write(self, absolute, data, mode, uid, gid):
        path = self.path(absolute)
        self.no_symlinks(path)
        fd, temporary = tempfile.mkstemp(prefix='.native-login-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
                os.fchown(target.fileno(), uid, gid)
                os.fchmod(target.fileno(), mode)
            os.link(temporary, path)  # Fails instead of replacing a racing file.
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)

    # -- desired state ------------------------------------------------------
    def compile_greeter_settings(self, templates):
        config = templates['greeter_config']
        with tempfile.TemporaryDirectory(prefix='native-greeter-dconf-') as directory:
            settings = Path(directory) / 'settings.d'
            (settings / 'locks').mkdir(parents=True)
            (settings / '00-native-greeter').write_text(config['dconf_settings'](Path(GREETER_DATA)))
            (settings / 'locks/native-greeter').write_text(config['DCONF_LOCKS'])
            database = Path(directory) / 'settings.db'
            self.run(['dconf', 'compile', database, settings])
            if not database.is_file() or not database.stat().st_size:
                raise RuntimeError('dconf did not produce a nonempty settings database')
            return database.read_bytes()

    def desired(self):
        """Return [(path, bytes, mode, uid, gid)] and the directories to create."""
        templates = load_templates(self.path(self.prefix))
        guard = templates['startup_guard']
        config = templates['greeter_config']
        root, gdm = (self.owner_uid, self.owner_gid), (self.owner_uid, self.gdm_gid)
        user = (self.account.pw_uid, self.account.pw_gid)
        shell = ('[Service]\nExecStart=\nExecStart=/usr/bin/python3 ' + self.prefix +
                 '/scripts/native-shell.py -- /usr/bin/gnome-shell --mode=%i\n' +
                 '# Queue a separate job: never wait for ScreenSaver while Shell is activating.\n' +
                 guard['guard_start_hook']())
        files = [
            (LOGIN_CONFIG, json.dumps({'user': self.account.pw_name}) + '\n', 0o644, *root),
            (GREETER_CONFIG + '/dconf-profile', config['dconf_profile'](Path(GREETER_CONFIG)), 0o644, *root),
            (GREETER_CONFIG + '/settings.db', self.compile_greeter_settings(templates), 0o644, *root),
            (GATEWAY, gateway_unit(self.prefix), 0o644, *root),
            (SHELL_DROPIN, shell, 0o644, *root),
            (GDM_SHELL_DROPIN, '[Service]\nExecStartPost=/usr/bin/systemctl --user --no-block '
                               'start ubuntu-native-greeter.service\n', 0o644, *root),
            (GREETER_UNIT, greeter_unit(self.prefix), 0o644, *root),
            (GUARD_UNIT, guard['guard_service_template'](self.prefix), 0o644, *root),
            (self.home + '/.config/systemd/user/ubuntu-native-takeover.service.d/95-system-login.conf',
             backend_override(self.prefix), 0o644, *user),
        ]
        source = self.home + '/.local/share/ubuntu-native-takeover/'
        for relative in SECRETS:
            path = self.path(source + relative)
            if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                raise RuntimeError('Missing desktop RDP asset (run control.py setup first): ' + source + relative)
            files.append((GREETER_DATA + '/' + relative, path.read_bytes(), 0o640, *gdm))
        directories = [
            ('/etc/ubuntu-native-login', 0o755, *root), (GREETER_CONFIG, 0o755, *root),
            (GREETER_DATA, 0o750, *gdm), (GREETER_DATA + '/rdp', 0o750, *gdm),
            (GREETER_DATA + '/rdp/gnome-remote-desktop', 0o750, *gdm),
            (str(PurePosixPath(SHELL_DROPIN).parent), 0o755, *root),
            (str(PurePosixPath(GDM_SHELL_DROPIN).parent), 0o755, *root),
            (self.home + '/.config/systemd/user/ubuntu-native-takeover.service.d', 0o755, *user),
        ]
        return [(p, d.encode() if isinstance(d, str) else d, m, u, g) for p, d, m, u, g in files], directories

    # -- preconditions ------------------------------------------------------
    def preflight(self):
        problems = []
        user_dropin = self.path(self.home + '/.config/systemd/user/org.gnome.Shell@ubuntu.service.d/90-native-takeover.conf')
        if not user_dropin.is_file() or self.prefix + '/scripts/native-shell.py' not in user_dropin.read_text():
            problems.append('Per-user setup for this prefix is missing: run control.py setup as the desktop user first')
        gdm = self.path(GDM_CONFIG)
        if gdm.is_file() and re.search(r'(?mi)^\s*(AutomaticLoginEnable|TimedLoginEnable)\s*=\s*(true|1)\s*$',
                                       gdm.read_text()):
            problems.append('GDM automatic/timed login is enabled in ' + GDM_CONFIG +
                            '; disable it, a remote GDM login requires the physical greeter')
        if self.path(MANIFEST).exists():
            problems.append('An install record already exists: ' + MANIFEST + ' (use rollback first)')
        return problems

    def plan(self):
        files, directories = self.desired()
        return {'user': self.account.pw_name, 'prefix': self.prefix,
                'problems': self.preflight(),
                'existing_destinations': [p for p, *_ in files if self.path(p).exists() or self.path(p).is_symlink()],
                'would_write': [p for p, *_ in files],
                'enables': 'ubuntu-native-login.service (port 3392)',
                'restarts_gdm_or_desktop': False}

    # -- actions ------------------------------------------------------------
    def save(self, manifest):
        self.write_manifest(json.dumps(manifest, indent=2) + '\n')

    def write_manifest(self, text):
        path = self.path(MANIFEST)
        fd, temporary = tempfile.mkstemp(prefix='.install-', dir=path.parent)
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def install(self, *, enable=True):
        problems = self.preflight()
        if problems:
            raise RuntimeError('Preflight failed:\n- ' + '\n- '.join(problems))
        files, directories = self.desired()
        existing = [p for p, *_ in files if self.path(p).exists() or self.path(p).is_symlink()]
        if existing:
            raise RuntimeError('Refusing to replace existing configuration: ' + ', '.join(existing))
        created = []
        self.make_directories(STATE, 0o700, self.owner_uid, self.owner_gid, [])
        # disable-login.py format: previous content (None = did not exist).
        manifest = {'version': 1, 'installer': 'install-system-login', 'user': self.account.pw_name,
                    'prefix': self.prefix, 'files': {}, 'installed_hashes': {},
                    'credentials': [], 'created_directories': created, 'phase': 'installing'}
        self.save(manifest)
        try:
            for absolute, mode, uid, gid in directories:
                self.make_directories(absolute, mode, uid, gid, created)
            self.save(manifest)
            for absolute, data, mode, uid, gid in files:
                self.write(absolute, data, mode, uid, gid)
                if absolute.startswith(GREETER_DATA + '/'):
                    # Retained by disable-login.py, removed by this rollback.
                    manifest['credentials'].append(absolute)
                else:
                    manifest['files'][absolute] = None
                    manifest['installed_hashes'][absolute] = sha(data)
                self.save(manifest)
            self.run(['systemctl', 'daemon-reload'])
            if enable:
                self.run(['systemctl', 'enable', '--now', 'ubuntu-native-login.service'])
            if self.path(f'/run/user/{self.account.pw_uid}/bus').exists():
                self.run(self.user_command(['systemctl', '--user', 'daemon-reload']))
        except Exception:
            manifest['phase'] = 'install-failed'
            self.save(manifest)
            raise
        manifest['phase'] = 'installed'
        self.save(manifest)
        return {'phase': 'installed', 'gateway_enabled': enable, 'gdm_restarted': False}

    def rollback(self, *, force=False):
        manifest_path = self.path(MANIFEST)
        manifest = json.loads(manifest_path.read_text())
        if manifest.get('installer') != 'install-system-login':
            raise RuntimeError('Install record was not written by this installer; use disable-login.py')
        changed = [p for p, h in manifest['installed_hashes'].items()
                   if self.path(p).exists() and sha(self.path(p).read_bytes()) != h]
        if changed and not force:
            raise RuntimeError('Configuration changed since install (use --force to remove anyway): ' + ', '.join(changed))
        self.runner(['systemctl', 'disable', '--now', 'ubuntu-native-login.service'],
                    check=False, capture_output=True, text=True, timeout=60)
        for absolute in [*manifest['files'], *manifest['credentials']]:
            path = self.path(absolute)
            self.no_symlinks(path)
            path.unlink(missing_ok=True)
        for absolute in sorted(manifest['created_directories'], key=lambda p: -len(PurePosixPath(p).parts)):
            with contextlib.suppress(FileNotFoundError, OSError):
                self.path(absolute).rmdir()  # Only if empty.
        self.runner(['systemctl', 'daemon-reload'], check=False, capture_output=True, text=True, timeout=60)
        if self.path(f'/run/user/{self.account.pw_uid}/bus').exists():
            self.runner(self.user_command(['systemctl', '--user', 'daemon-reload']),
                        check=False, capture_output=True, text=True, timeout=60)
        manifest_path.rename(manifest_path.with_name('install.rolled-back.json'))
        return {'phase': 'rolled-back', 'gdm_restarted': False,
                'next_step': 'sudo systemctl restart gdm (only when no local desktop work is open)'}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('plan', 'install', 'rollback'))
    parser.add_argument('--user', required=True, help='desktop account configured by control.py setup')
    parser.add_argument('--no-enable', action='store_true', help='write files but do not enable the gateway')
    parser.add_argument('--force', action='store_true', help='rollback even if installed files changed')
    args = parser.parse_args()
    if args.command != 'plan' and os.geteuid() != 0:
        raise SystemExit('Run install/rollback with sudo; plan needs no root.')
    installer = Installer(args.user)
    if args.command == 'plan':
        result = installer.plan()
    elif args.command == 'install':
        result = installer.install(enable=not args.no_enable)
    else:
        result = installer.rollback(force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, KeyError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
