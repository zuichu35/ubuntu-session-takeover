#!/usr/bin/python3
"""Stage the versioned 3392 login fix without restarting the desktop.

Run plan without sudo. install stages files and reloads unit definitions only.
activate-gateway is a separate, idle-only gateway restart. repair-gateway updates
only the verified gateway module and then activates it. rollback restores
recorded configuration, retaining the versioned libraries and credentials.
"""
import argparse
import contextlib
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import time


VERSION = '20261028'
PREFIX = '/opt/ubuntu-native-takeover-login-' + VERSION
STATE = '/var/lib/ubuntu-native-login/greeter-fix-' + VERSION
GATEWAY = '/etc/systemd/system/ubuntu-native-login.service'
GREETER_DATA = '/var/lib/ubuntu-native-greeter'
GREETER_CONFIG = '/etc/ubuntu-native-login/greeter'
SECRETS = ('tls.crt', 'tls.key', 'rdp/gnome-remote-desktop/credentials.ini')
GATEWAY_MODULE = 'takeover/login_gateway.py'


class GatewayJobPending(RuntimeError):
    """A systemd job still owns the gateway; never submit a competing restart."""


def bounded_gateway_stop(unit):
    if re.search(r'(?m)^TimeoutStopSec=', unit):
        return re.sub(r'(?m)^TimeoutStopSec=.*$', 'TimeoutStopSec=10s', unit)
    return unit.rstrip() + '\n\n[Service]\nTimeoutStopSec=10s\n'


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def checked_relative(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or str(path) != value:
        raise RuntimeError('Unsafe manifest path: ' + value)
    return path


def metadata(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError('Expected a regular configuration file: ' + str(path))
    return {'sha256': digest(path), 'uid': info.st_uid, 'gid': info.st_gid,
            'mode': stat.S_IMODE(info.st_mode)}


class Installer:
    def __init__(self, package, *, root=Path('/'), runner=subprocess.run,
                 account_lookup=pwd.getpwnam, owner_uid=0, owner_gid=0, gdm_gid=None):
        self.package, self.root = Path(package).resolve(), Path(root).resolve()
        self.runner, self.account_lookup = runner, account_lookup
        self.owner_uid, self.owner_gid = owner_uid, owner_gid
        self.gdm_gid = grp.getgrnam('gdm').gr_gid if gdm_gid is None else gdm_gid
        self.state_dir = self.path(STATE)
        self.state_file = self.state_dir / 'state.json'
        config = json.loads(self.path('/etc/ubuntu-native-login/config.json').read_text())
        self.account = account_lookup(config['user'])
        if self.account.pw_uid < 1000:
            raise RuntimeError('The configured desktop account must be a regular user')
        self.home = self.account.pw_dir

    def path(self, absolute):
        value = PurePosixPath(absolute)
        if not value.is_absolute() or '..' in value.parts:
            raise RuntimeError('Expected an absolute destination path')
        return self.root / str(value).lstrip('/')

    def run(self, args, *, check=True, timeout=30):
        result = self.runner([str(arg) for arg in args], check=False,
                             capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            # Command output can include credentials for arbitrary tools. Only
            # our fixed commands are run, but still report a bounded diagnostic.
            raise RuntimeError(f'{args[0]} failed ({result.returncode}): {result.stderr[-2000:]}')
        return result.stdout

    def user_command(self, args):
        uid = self.account.pw_uid
        return ['runuser', '-u', self.account.pw_name, '--', 'env',
                f'XDG_RUNTIME_DIR=/run/user/{uid}',
                f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus', *args]

    def no_symlink_path(self, path):
        for part in (path, *path.parents):
            if part.is_symlink():
                raise RuntimeError('Refusing a symlink in a destination path: ' + str(part))
            if part == self.root:
                break

    def ensure_directory(self, path, mode=0o755, uid=None, gid=None):
        self.no_symlink_path(path)
        missing = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent
        if not current.is_dir():
            raise RuntimeError('Destination ancestor is not a directory: ' + str(current))
        for directory in reversed(missing):
            directory.mkdir(mode=mode)
            os.chown(directory, self.owner_uid if uid is None else uid,
                     self.owner_gid if gid is None else gid)
            directory.chmod(mode)

    def write(self, absolute, data, *, mode=0o644, uid=None, gid=None):
        path = self.path(absolute)
        uid = self.owner_uid if uid is None else uid
        gid = self.owner_gid if gid is None else gid
        self.no_symlink_path(path)
        if path.exists() and not path.is_file():
            raise RuntimeError('Refusing to replace a non-file: ' + str(path))
        self.ensure_directory(path.parent, uid=uid, gid=gid)
        fd, temporary = tempfile.mkstemp(prefix='.native-login-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as target:
                target.write(data.encode() if isinstance(data, str) else data)
                target.flush()
                os.fsync(target.fileno())
                os.fchown(target.fileno(), uid, gid)
                os.fchmod(target.fileno(), mode)
            os.replace(temporary, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)

    @contextlib.contextmanager
    def lock(self):
        self.ensure_directory(self.state_dir, mode=0o700)
        os.chown(self.state_dir, self.owner_uid, self.owner_gid)
        self.state_dir.chmod(0o700)
        with (self.state_dir / 'lock').open('a') as handle:
            os.chmod(handle.name, 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield

    def save(self, state):
        self.write(STATE + '/state.json', json.dumps(state, indent=2) + '\n', mode=0o600)

    def load(self):
        return json.loads(self.state_file.read_text())

    def verify_bundle(self):
        manifest_path = self.package / 'payload-manifest.json'
        manifest = json.loads(manifest_path.read_text())
        if manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict):
            raise RuntimeError('Unsupported payload manifest')
        files, links = manifest['files'], manifest.get('symlinks', {})
        if not isinstance(links, dict) or not files or set(files) & set(links):
            raise RuntimeError('Invalid or empty payload manifest')
        payload = self.package / 'payload'
        if payload.is_symlink() or not payload.is_dir():
            raise RuntimeError('Payload must be a real directory')
        expected = set(files) | set(links)
        found = set()
        for current, directories, names in os.walk(payload, followlinks=False):
            for name in list(directories):
                path = Path(current) / name
                if path.is_symlink():
                    found.add(path.relative_to(payload).as_posix())
                    directories.remove(name)
            for name in names:
                found.add((Path(current) / name).relative_to(payload).as_posix())
        if found != expected:
            raise RuntimeError('Payload file inventory differs from its manifest')
        for relative, expected_hash in files.items():
            path = payload / checked_relative(relative)
            if path.is_symlink() or not path.is_file() or not re.fullmatch('[0-9a-f]{64}', expected_hash):
                raise RuntimeError('Invalid payload file entry: ' + relative)
            if digest(path) != expected_hash:
                raise RuntimeError('Payload checksum mismatch: ' + relative)
        for relative, target in links.items():
            path = payload / checked_relative(relative)
            if not isinstance(target, str) or PurePosixPath(target).is_absolute():
                raise RuntimeError('Invalid payload symlink target: ' + relative)
            if not path.is_symlink() or os.readlink(path) != target or not path.exists():
                raise RuntimeError('Payload symlink mismatch: ' + relative)
            if not path.resolve().is_relative_to(payload.resolve()):
                raise RuntimeError('Payload symlink escapes its root: ' + relative)
        abi = json.loads((self.package / 'system-abi.json').read_text())
        if not isinstance(abi, dict) or not abi:
            raise RuntimeError('System ABI fingerprints are missing')
        for absolute, expected_hash in abi.items():
            path = self.path(absolute)
            if not path.is_file() or digest(path) != expected_hash:
                raise RuntimeError('System ABI changed; rebuild the candidate before installing: ' + absolute)
        return manifest, digest(manifest_path)

    def verify_prefix(self, manifest):
        prefix = self.path(PREFIX)
        self.no_symlink_path(prefix)
        for relative, expected_hash in manifest['files'].items():
            path = prefix / relative
            if path.is_symlink() or not path.is_file() or digest(path) != expected_hash:
                raise RuntimeError('Installed candidate changed: ' + relative)
        for relative, target in manifest.get('symlinks', {}).items():
            path = prefix / relative
            if not path.is_symlink() or os.readlink(path) != target:
                raise RuntimeError('Installed candidate symlink changed: ' + relative)
        for current, directories, files in os.walk(prefix, followlinks=False):
            for path in [Path(current), *(Path(current) / name for name in directories + files)]:
                info = path.lstat()
                if info.st_uid != self.owner_uid or (not path.is_symlink() and info.st_mode & 0o022):
                    raise RuntimeError('Installed candidate is not exclusively root-managed: ' + str(path))

    def assert_current(self, state):
        for absolute, expected in state['installed'].items():
            path = self.path(absolute)
            if not path.exists() or metadata(path) != expected:
                raise RuntimeError('Installed configuration was changed; refusing to overwrite: ' + absolute)
        self.verify_prefix(state['payload_manifest'])

    def reload_units(self):
        self.run(['systemctl', 'daemon-reload'])
        if self.path(f'/run/user/{self.account.pw_uid}/bus').exists():
            self.run(self.user_command(['systemctl', '--user', 'daemon-reload']))

    def desired_units(self):
        # Load the same reviewed templates used by the runtime/build, without
        # writing an import cache into the checksum-verified payload.
        guard_source = self.package / 'payload/takeover/startup_guard.py'
        if guard_source.is_symlink() or not guard_source.is_file():
            raise RuntimeError('Bundle lacks startup_guard.py; rebuild with the asynchronous startup coordinator')
        guard_templates = {}
        exec(compile(guard_source.read_text(), str(guard_source), 'exec'), guard_templates)
        old_gateway = self.path(GATEWAY).read_text()
        gateway, count = re.subn(r'(?m)^WorkingDirectory=.*$', 'WorkingDirectory=' + PREFIX, old_gateway)
        if count != 1 or 'ExecStart=/usr/bin/python3 -m takeover.login_gateway' not in gateway:
            raise RuntimeError('Unexpected existing gateway unit; refusing to replace its command')
        gateway = bounded_gateway_stop(gateway)
        shell = ('[Service]\nExecStart=\nExecStart=/usr/bin/python3 ' + PREFIX +
                 '/scripts/native-shell.py -- /usr/bin/gnome-shell --mode=%i\n')
        units = {
            GATEWAY: gateway,
            '/etc/systemd/user/org.gnome.Shell@.service.d/80-native-login.conf': shell +
                guard_templates['guard_start_hook'](),
            '/etc/systemd/user/ubuntu-native-login-guard.service':
                guard_templates['guard_service_template'](PREFIX),
            '/etc/systemd/user/ubuntu-native-greeter.service': f'''[Unit]
Description=RDP capture of the actual native GDM greeter
ConditionGroup=gdm
After=org.gnome.Shell@gdm.service
PartOf=gnome-session@gnome-login.target
[Service]
Type=simple
ExecStart=/usr/bin/python3 {PREFIX}/scripts/native-session.py --greeter
Restart=on-failure
RestartSec=3
WatchdogSec=60
NotifyAccess=main
KillMode=control-group
TimeoutStopSec=20
''',
            self.home + '/.config/systemd/user/org.gnome.Shell@ubuntu.service.d/90-native-takeover.conf': shell,
            self.home + '/.config/systemd/user/ubuntu-native-takeover.service.d/95-system-login.conf': f'''[Service]
ExecStart=
ExecStart=/usr/bin/python3 {PREFIX}/scripts/native-session.py
Restart=on-failure
RestartSec=3
WatchdogSec=60
NotifyAccess=main
''',
        }
        return units

    def plan(self):
        manifest, bundle_hash = self.verify_bundle()
        try:
            existing_state = self.load()['phase'] if self.state_file.exists() else None
        except PermissionError:
            existing_state = 'root-owned installation record (sudo required to inspect)'
        return {'version': VERSION, 'user': self.account.pw_name, 'prefix': PREFIX,
                'payload_files': len(manifest['files']), 'bundle_sha256': bundle_hash,
                'unit_destinations': sorted(self.desired_units()),
                'credential_destination': GREETER_DATA,
                'configuration_destination': GREETER_CONFIG,
                'install_restarts_services': False,
                'existing_state': existing_state}

    def stage_prefix(self, manifest):
        destination = self.path(PREFIX)
        if destination.exists() or destination.is_symlink():
            raise RuntimeError('Versioned candidate already exists without this installation record')
        self.ensure_directory(destination.parent)
        temporary = Path(tempfile.mkdtemp(prefix='.native-login-candidate-', dir=destination.parent))
        try:
            shutil.copytree(self.package / 'payload', temporary, symlinks=True, dirs_exist_ok=True)
            for current, directories, files in os.walk(temporary, followlinks=False):
                for path in [Path(current), *(Path(current) / name for name in directories + files)]:
                    os.chown(path, self.owner_uid, self.owner_gid, follow_symlinks=False)
                    if not path.is_symlink():
                        path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
            self.verify_candidate_compatibility(temporary)
            os.rename(temporary, destination)
            self.verify_prefix(manifest)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def verify_candidate_compatibility(self, staged):
        """Gate new-policy bundles before changing the installed runtime or units.

        Historical bundles without a compatibility policy retain their original
        checks. New launchers always ship this module and need final-payload
        evidence; a developer-owner inspection is never deployment approval.
        """
        source = staged / 'takeover/compatibility.py'
        if not source.exists():
            return
        namespace = {'__file__': str(source), '__name__': 'staged_compatibility'}
        exec(compile(source.read_text(), str(source), 'exec'), namespace)
        report = namespace['inspect_candidate'](staged, required_owner=0,
            system_root=self.root, runtime_prefix=PREFIX)
        if report.get('status') != 'verified' or report.get('production_eligible') is not True:
            raise RuntimeError('Final candidate compatibility is not verified: ' + json.dumps(report))

    def prepare_files(self, temporary):
        # The module is part of the checksum-verified payload and only supplies
        # the shared static dconf format used by the runtime validator.
        source = self.package / 'payload/takeover/greeter_config.py'
        config = {}
        exec(compile(source.read_text(), str(source), 'exec'), config)
        settings = temporary / 'settings.d'
        settings.mkdir()
        (settings / '00-native-greeter').write_text(config['dconf_settings'](Path(GREETER_DATA)))
        (settings / 'locks').mkdir()
        (settings / 'locks/native-greeter').write_text(config['DCONF_LOCKS'])
        database = temporary / 'settings.db'
        self.run(['dconf', 'compile', database, settings])
        if not database.is_file() or not database.stat().st_size:
            raise RuntimeError('dconf did not produce a nonempty settings database')
        result = {}
        for name, content in self.desired_units().items():
            user_owned = name.startswith(self.home + '/')
            result[name] = {'data': content, 'mode': 0o644,
                'uid': self.account.pw_uid if user_owned else self.owner_uid,
                'gid': self.account.pw_gid if user_owned else self.owner_gid, 'retain': False}
        result[GREETER_CONFIG + '/dconf-profile'] = {
            'data': config['dconf_profile'](Path(GREETER_CONFIG)), 'mode': 0o644, 'retain': False}
        result[GREETER_CONFIG + '/settings.db'] = {
            'data': database.read_bytes(), 'mode': 0o644, 'retain': False}
        for relative in SECRETS:
            source = self.path(self.home + '/.local/share/ubuntu-native-takeover/' + relative)
            if source.is_symlink() or not source.is_file() or not source.stat().st_size:
                raise RuntimeError('Missing or invalid existing RDP credential asset: ' + relative)
            result[GREETER_DATA + '/' + relative] = {
                'data': source.read_bytes(), 'mode': 0o640,
                'gid': self.gdm_gid, 'retain': True}
        return result

    def install(self):
        manifest, bundle_hash = self.verify_bundle()
        if self.state_file.exists():
            state = self.load()
            if state['bundle_sha256'] != bundle_hash or state['phase'] not in ('installed', 'active', 'gateway-fallback'):
                raise RuntimeError('Existing installation state requires review; no files were overwritten')
            self.assert_current(state)
            return {'phase': state['phase'], 'idempotent': True}
        for absolute in (GREETER_CONFIG, GREETER_DATA):
            if self.path(absolute).exists() or self.path(absolute).is_symlink():
                raise RuntimeError('Dedicated greeter destination already exists without this version record; refusing to replace unknown configuration or credentials: ' + absolute)
        self.ensure_directory(self.state_dir, mode=0o700)
        os.chown(self.state_dir, self.owner_uid, self.owner_gid)
        self.state_dir.chmod(0o700)
        with tempfile.TemporaryDirectory(prefix='prepare-', dir=self.state_dir) as directory:
            desired = self.prepare_files(Path(directory))
        state = {'version': VERSION, 'phase': 'preparing', 'user': self.account.pw_name,
                 'bundle_sha256': bundle_hash, 'payload_manifest': manifest,
                 'original': {}, 'installed': {}, 'retained': [], 'gateway_active': False,
                 'desired_gateway': desired[GATEWAY]['data']}
        backups = self.state_dir / 'backups'
        self.ensure_directory(backups, mode=0o700)
        os.chown(backups, self.owner_uid, self.owner_gid)
        backups.chmod(0o700)
        for index, (absolute, item) in enumerate(desired.items()):
            path = self.path(absolute)
            self.no_symlink_path(path)
            original = {'exists': path.exists()}
            if original['exists']:
                original.update(metadata(path))
                original['backup'] = f'backups/{index}'
                backup = self.state_dir / original['backup']
                backup.write_bytes(path.read_bytes())
                backup.chmod(0o600)
            state['original'][absolute] = original
            if item['retain']:
                state['retained'].append(absolute)
        self.stage_prefix(manifest)
        state['phase'] = 'installing'
        self.save(state)
        try:
            for directory in (GREETER_CONFIG,):
                self.ensure_directory(self.path(directory))
                os.chown(self.path(directory), self.owner_uid, self.owner_gid)
                self.path(directory).chmod(0o755)
            for directory in (GREETER_DATA, GREETER_DATA + '/rdp', GREETER_DATA + '/rdp/gnome-remote-desktop'):
                self.ensure_directory(self.path(directory), mode=0o750, gid=self.gdm_gid)
                os.chown(self.path(directory), self.owner_uid, self.gdm_gid)
                self.path(directory).chmod(0o750)
            for absolute, item in desired.items():
                self.write(absolute, item['data'], mode=item['mode'], uid=item.get('uid'), gid=item.get('gid'))
                state['installed'][absolute] = metadata(self.path(absolute))
                self.save(state)
            self.reload_units()
            state['phase'] = 'installed'
            self.save(state)
        except Exception:
            self.restore_configuration(state)
            with contextlib.suppress(Exception):
                self.reload_units()
            state['phase'] = 'install-failed-restored'
            self.save(state)
            raise
        return {'phase': 'installed', 'services_restarted': False,
                'next_step': 'activate-gateway only while the desktop is locally idle'}

    def restore_file(self, absolute, record):
        if record['exists']:
            backup = self.state_dir / record['backup']
            if digest(backup) != record['sha256']:
                raise RuntimeError('Original backup checksum mismatch: ' + absolute)
            self.write(absolute, backup.read_bytes(), mode=record['mode'], uid=record['uid'], gid=record['gid'])
        else:
            self.no_symlink_path(self.path(absolute))
            self.path(absolute).unlink(missing_ok=True)

    def restore_configuration(self, state):
        # Verify every backup before restoring the first file, so a damaged
        # later backup cannot leave half of the unit set rolled back.
        for absolute in state['installed']:
            record = state['original'][absolute]
            if absolute not in state['retained'] and record['exists']:
                if digest(self.state_dir / record['backup']) != record['sha256']:
                    raise RuntimeError('Original backup checksum mismatch: ' + absolute)
        for absolute in state['installed']:
            if absolute not in state['retained']:
                self.restore_file(absolute, state['original'][absolute])

    def unit_properties(self, name, user=False):
        command = ['systemctl'] + (['--user'] if user else []) + [
            'show', name, '-p', 'MainPID', '-p', 'ActiveState', '-p', 'SubState',
            '-p', 'Job', '-p', 'InvocationID', '-p', 'Result', '-p', 'TimeoutStopUSec']
        output = self.run(self.user_command(command) if user else command)
        return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)

    def running_gateway_is_candidate(self):
        # install affects the next boot/automatic service restart even when the
        # explicit activation command has never run. Inspect the actual process,
        # not systemctl's newly reloaded WorkingDirectory or our saved flag.
        unit = self.unit_properties('ubuntu-native-login.service')
        pid = int(unit.get('MainPID', '0'))
        if unit.get('ActiveState') != 'active' or pid <= 0:
            return False
        try:
            working_directory = self.path(f'/proc/{pid}/cwd').resolve(strict=True)
            arguments = self.path(f'/proc/{pid}/cmdline').read_bytes().decode().split('\0')
        except (OSError, UnicodeError) as error:
            raise RuntimeError('Cannot identify the active gateway process; rollback was not attempted') from error
        return working_directory == self.path(PREFIX).resolve() or any(
            argument.startswith(PREFIX + '/') for argument in arguments)

    def broker_status(self):
        code = '''import json,os,socket
s=socket.socket(socket.AF_UNIX);s.settimeout(3)
s.connect('/run/user/'+str(os.getuid())+'/ubuntu-native-takeover/gate.sock')
s.sendall(b'STATUS\\n');data=b''
while b'\\n' not in data and len(data)<4096:
 chunk=s.recv(4096-len(data))
 if not chunk: raise RuntimeError('Broker closed without STATUS')
 data+=chunk
s.close()
if b'\\n' not in data: raise RuntimeError('Oversized broker status')
print(json.dumps(json.loads(data.split(b'\\n',1)[0])))
'''
        return json.loads(self.run(self.user_command(['/usr/bin/python3', '-c', code])))

    def idle_snapshot(self, require_idle=True):
        broker = self.broker_status()
        if require_idle and (broker.get('native_state') != 'local' or broker.get('remote') is not False):
            raise RuntimeError('Gateway change requires an idle local desktop, with no remote lease')
        if require_idle and self.run(['ss', '-Htn', 'state', 'established', '( sport = :3392 or sport = :3393 )']).strip():
            raise RuntimeError('A 3392/3393 connection is established; gateway change was not attempted')
        shell = self.unit_properties('org.gnome.Shell@ubuntu.service', user=True)
        backend = self.unit_properties('ubuntu-native-takeover.service', user=True)
        if any(unit.get('ActiveState') != 'active' or int(unit.get('MainPID', '0')) <= 0
               for unit in (shell, backend)):
            raise RuntimeError('An active physical GNOME Shell and native backend are required')
        pid = int(backend['MainPID'])
        arguments = self.path(f'/proc/{pid}/cmdline').read_bytes().decode().split('\0')
        candidates = [value for value in arguments if value.endswith('/native-session.py')]
        if len(candidates) != 1:
            raise RuntimeError('Cannot identify the running native supervisor')
        supervisor = self.path(candidates[0])
        helper = supervisor.parent.parent / 'takeover/session_health.py'
        if 'GatewayRegistration(' not in supervisor.read_text() or 'class GatewayRegistration:' not in helper.read_text():
            raise RuntimeError('Running supervisor lacks gateway re-registration; refusing a disruptive restart')
        proc_stat = self.path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        started = time.time() - float(self.path('/proc/uptime').read_text().split()[0]) + \
                  int(proc_stat[19]) / os.sysconf('SC_CLK_TCK')
        if max(supervisor.stat().st_mtime, helper.stat().st_mtime) > started + 1:
            raise RuntimeError('Supervisor source changed after startup; running recovery support is unverified')
        children = self.path(f'/proc/{pid}/task/{pid}/children').read_text().split()
        grd = []
        for child in children:
            command = self.path(f'/proc/{child}/cmdline').read_bytes().split(b'\0')[0]
            if command.endswith(b'/gnome-remote-desktop-daemon'):
                grd.append(int(child))
        if len(grd) != 1:
            raise RuntimeError('Cannot identify the single native RDP backend process')
        return {'gnome_pid': int(shell['MainPID']), 'supervisor_pid': pid,
                'backend_pid': grd[0], 'session': broker.get('session')}

    def gateway_status(self):
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(2)
            connection.connect(str(self.path('/run/ubuntu-native-login/control.sock')))
            connection.sendall(b'{"command":"status"}\n')
            response = b''
            while b'\n' not in response and len(response) < 4096:
                chunk = connection.recv(4096 - len(response))
                if not chunk:
                    raise RuntimeError('Gateway closed without status')
                response += chunk
            if b'\n' not in response:
                raise RuntimeError('Oversized gateway status')
            return json.loads(response.split(b'\n', 1)[0])

    def wait_gateway(self, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status = self.gateway_status()
                if status.get('version') == 1 and 'desktop' in status.get('endpoints', []):
                    return status
            except (OSError, ValueError, RuntimeError):
                pass
            time.sleep(.25)
        raise RuntimeError('New gateway did not receive the desktop registration before timeout')

    @staticmethod
    def gateway_job_pending(properties):
        job = (properties.get('Job') or '0').split()[0]
        return job not in ('0', '') or properties.get('ActiveState') in (
            'activating', 'deactivating', 'reloading')

    def wait_gateway_job(self, timeout=130, *, require_active=False, previous=None):
        """Wait for systemd itself, including an old unit's 90-second stop.

        A command timeout is not proof of job completion. Keep the job intact
        and report pending state instead of queuing a conflicting fallback.
        """
        deadline = time.monotonic() + timeout
        properties = {}
        while time.monotonic() < deadline:
            properties = self.unit_properties('ubuntu-native-login.service')
            if not self.gateway_job_pending(properties):
                if not require_active:
                    return properties
                if (properties.get('ActiveState') != 'active' or
                        properties.get('SubState') != 'running' or
                        int(properties.get('MainPID', '0')) <= 0):
                    raise RuntimeError('Gateway restart job finished unsuccessfully: ' + json.dumps(properties))
                changed = not previous or (
                    properties.get('MainPID') != previous.get('MainPID') or
                    properties.get('InvocationID') != previous.get('InvocationID'))
                if changed:
                    return properties
            time.sleep(.25)
        if self.gateway_job_pending(properties):
            raise GatewayJobPending('Gateway systemd job is still pending after ' + str(timeout) +
                                    ' seconds; no competing restart was submitted: ' + json.dumps(properties))
        raise RuntimeError('Gateway restart did not produce a new service invocation')

    def restart_gateway(self):
        previous = self.unit_properties('ubuntu-native-login.service')
        if self.gateway_job_pending(previous):
            raise GatewayJobPending('An existing gateway systemd job is still pending; no restart was submitted')
        try:
            self.run(['systemctl', '--no-block', 'restart', 'ubuntu-native-login.service'])
        except subprocess.TimeoutExpired:
            # D-Bus may have accepted the job before the client timed out.
            # Observe that job and its new invocation instead of submitting again.
            pass
        return self.wait_gateway_job(require_active=True, previous=previous)

    def repair_gateway(self):
        state = self.load()
        if state['phase'] not in ('installed', 'active', 'gateway-fallback',
                                  'gateway-pending', 'gateway-repairing'):
            raise RuntimeError('Install must complete before gateway repair')
        manifest, bundle_hash = self.verify_bundle()
        same_bundle = state['bundle_sha256'] == bundle_hash
        if same_bundle:
            if state['payload_manifest'] != manifest:
                raise RuntimeError('Installation manifest does not match its recorded bundle checksum')
            self.assert_current(state)
        else:
            previous = self.package / 'previous-payload-manifest.json'
            if (previous.is_symlink() or not previous.is_file() or
                    digest(previous) != state['bundle_sha256'] or
                    json.loads(previous.read_text()) != state['payload_manifest']):
                raise RuntimeError('Previous payload manifest does not match the root-owned installation record')
            old = state['payload_manifest']
            if (GATEWAY_MODULE not in old['files'] or GATEWAY_MODULE not in manifest['files'] or
                    {key: value for key, value in old.items() if key != 'files'} !=
                    {key: value for key, value in manifest.items() if key != 'files'} or
                    {key: value for key, value in old['files'].items() if key != GATEWAY_MODULE} !=
                    {key: value for key, value in manifest['files'].items() if key != GATEWAY_MODULE}):
                raise RuntimeError('Gateway repair may change only takeover/login_gateway.py')
            module = self.path(PREFIX + '/' + GATEWAY_MODULE)
            repair = state.get('gateway_repair', {})
            if (state['phase'] == 'gateway-repairing' and repair.get('to_bundle') == bundle_hash and
                    digest(module) == manifest['files'][GATEWAY_MODULE]):
                # Resume after interruption between atomic source replacement
                # and state commit; every other installed file must still match.
                self.assert_current(dict(state, payload_manifest=manifest))
            else:
                self.assert_current(state)
            self.wait_gateway_job()
            self.idle_snapshot()
            if state['phase'] != 'gateway-repairing':
                original = metadata(module)
                backup = 'gateway-repair/login_gateway-' + original['sha256'] + '.py'
                backup_path = self.state_dir / backup
                if backup_path.exists() and digest(backup_path) != original['sha256']:
                    raise RuntimeError('Gateway repair backup checksum mismatch')
                self.write(STATE + '/' + backup, module.read_bytes(), mode=0o600)
                repair = {'status': 'prepared', 'backup': backup, 'original': original,
                          'from_bundle': state['bundle_sha256'], 'to_bundle': bundle_hash}
                state.update(phase='gateway-repairing', gateway_repair=repair)
                self.save(state)
            backup = self.state_dir / repair['backup']
            if digest(backup) != repair['original']['sha256']:
                raise RuntimeError('Gateway repair backup checksum mismatch')
            self.write(PREFIX + '/' + GATEWAY_MODULE,
                       (self.package / 'payload' / GATEWAY_MODULE).read_bytes(),
                       mode=repair['original']['mode'])
            state.update(payload_manifest=manifest, bundle_sha256=bundle_hash,
                         gateway_active=False, phase='installed')
            state['gateway_repair']['status'] = 'complete'
        state['desired_gateway'] = bounded_gateway_stop(state['desired_gateway'])
        self.save(state)
        result = self.activate_gateway()
        result['gateway_repair'] = 'already-applied' if same_bundle else 'applied'
        return result

    def activate_gateway(self):
        state = self.load()
        if state['phase'] not in ('installed', 'active', 'gateway-fallback', 'gateway-pending'):
            raise RuntimeError('Install must complete before gateway activation')
        self.assert_current(state)
        if state['gateway_active']:
            status = self.gateway_status()
            if status.get('version') != 1 or 'desktop' not in status.get('endpoints', []):
                raise RuntimeError('Recorded active gateway is not reporting a registered desktop')
            return {'phase': 'active', 'idempotent': True, 'gateway': status}
        self.wait_gateway_job()
        before = self.idle_snapshot()
        # A previous failed activation may have restored the old unit.
        self.write(GATEWAY, state['desired_gateway'])
        state['installed'][GATEWAY] = metadata(self.path(GATEWAY))
        self.save(state)
        self.run(['systemctl', 'daemon-reload'])
        if self.unit_properties('ubuntu-native-login.service').get('TimeoutStopUSec') != '10s':
            raise RuntimeError('Gateway TimeoutStopSec=10s was not loaded; gateway was not restarted')
        if self.idle_snapshot() != before:
            raise RuntimeError('Desktop state changed during preflight; gateway was not restarted')
        try:
            self.restart_gateway()
            status = self.wait_gateway()
            after = self.idle_snapshot(require_idle=False)
            if after != before:
                raise RuntimeError('Desktop/backend identity changed during gateway activation')
        except Exception as failure:
            pending = failure if isinstance(failure, GatewayJobPending) else None
            if pending is None:
                try:
                    self.wait_gateway_job()
                except Exception as error:
                    pending = error
            if pending is not None:
                state.update(phase='gateway-pending', gateway_active=False,
                             activation_error=str(failure), activation_before=before)
                self.save(state)
                raise RuntimeError('Gateway job remains pending; original unit was not restored and no competing fallback restart was submitted. Rerun repair-gateway or activate-gateway after it finishes: ' + str(pending)) from failure
            self.restore_file(GATEWAY, state['original'][GATEWAY])
            state['installed'][GATEWAY] = metadata(self.path(GATEWAY))
            state.update(phase='gateway-fallback', gateway_active=False,
                         activation_error=str(failure), activation_before=before)
            self.save(state)
            try:
                self.run(['systemctl', 'daemon-reload'])
                self.restart_gateway()
            except Exception as recovery_failure:
                state['fallback_error'] = str(recovery_failure)
                self.save(state)
                raise RuntimeError('New gateway activation failed and restarting the restored original gateway also failed: ' + str(recovery_failure)) from failure
            raise RuntimeError('New gateway activation failed; the original gateway unit was restored and restarted: ' + str(failure)) from failure
        state.update(phase='active', gateway_active=True, activation_before=before,
                     activation_after=after)
        state.pop('activation_error', None)
        state.pop('fallback_error', None)
        self.save(state)
        return {'phase': 'active', 'gateway': status, 'processes_unchanged': after}

    def rollback(self):
        state = self.load()
        if state['phase'] == 'rolled-back':
            return {'phase': 'rolled-back', 'idempotent': True}
        if state['phase'] == 'rollback-pending-gateway':
            for absolute, original in state['original'].items():
                if absolute in state['retained']:
                    continue
                path = self.path(absolute)
                if original['exists']:
                    expected = {key: original[key] for key in ('sha256', 'uid', 'gid', 'mode')}
                    if not path.exists() or metadata(path) != expected:
                        raise RuntimeError('Restored configuration changed; refusing deferred gateway restart: ' + absolute)
                elif path.exists() or path.is_symlink():
                    raise RuntimeError('Restored configuration changed; refusing deferred gateway restart: ' + absolute)
            self.idle_snapshot()
            self.wait_gateway_job()
            self.restart_gateway()
            state.update(phase='rolled-back', gateway_active=False)
            self.save(state)
            return {'phase': 'rolled-back', 'candidate_and_credentials_retained': True,
                    'gateway_restarted': True}
        if state['phase'] not in ('installed', 'active', 'gateway-fallback', 'gateway-pending'):
            raise RuntimeError('Incomplete installation state requires review before rollback')
        self.assert_current(state)
        needs_restart = state['gateway_active'] or self.running_gateway_is_candidate()
        before = self.idle_snapshot() if needs_restart else None
        self.restore_configuration(state)
        self.reload_units()
        if needs_restart:
            state['phase'] = 'rollback-pending-gateway'
            self.save(state)
            try:
                if self.idle_snapshot() != before:
                    raise RuntimeError('Desktop identity changed')
                self.wait_gateway_job()
                self.restart_gateway()
            except Exception as error:
                raise RuntimeError('Configuration restored; rerun rollback when locally idle to finish the gateway restart: ' + str(error)) from error
        state.update(phase='rolled-back', gateway_active=False)
        self.save(state)
        return {'phase': 'rolled-back', 'candidate_and_credentials_retained': True,
                'gateway_restarted': before is not None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'install', 'activate-gateway', 'repair-gateway', 'rollback'))
    args = parser.parse_args()
    if args.command != 'plan' and os.geteuid() != 0:
        raise SystemExit('Run this command with sudo in a normal terminal; plan needs no sudo.')
    installer = Installer(Path(__file__).resolve().parent)
    if args.command == 'plan':
        result = installer.plan()
    else:
        with installer.lock():
            result = getattr(installer, args.command.replace('-', '_'))()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
