#!/usr/bin/python3
"""Atomically repair GRD resources/readiness without changing any service.

The original install record and an exact old manifest anchor authorize this
limited update. Existing processes keep their mapped executable; no service is
started, stopped, restarted or reloaded by either command.
"""
import argparse
import contextlib
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile


HERE = Path(__file__).resolve().parent
BASE_PATH = HERE / ('install.py' if (HERE / 'payload-manifest.json').is_file()
                    else 'install-greeter-fix.py')
SPEC = importlib.util.spec_from_file_location('native_login_base_installer', BASE_PATH)
BASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASE)

ANCHOR = 'before-greeter-startup-manifest.json'
RECORD = 'startup-fix'
HEALTH = 'takeover/session_health.py'
GRD = 'build/grd-prefix/'


def allowed(relative):
    BASE.checked_relative(relative)
    return relative == HEALTH or relative.startswith(GRD)


def entries(manifest):
    result = {path: {'kind': 'file', 'sha256': checksum}
              for path, checksum in manifest['files'].items()}
    result.update({path: {'kind': 'symlink', 'target': target}
                   for path, target in manifest.get('symlinks', {}).items()})
    return result


def fingerprint(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {'kind': 'absent'}
    common = {'uid': info.st_uid, 'gid': info.st_gid, 'mode': stat.S_IMODE(info.st_mode)}
    if stat.S_ISREG(info.st_mode):
        return dict(common, kind='file', sha256=BASE.digest(path))
    if stat.S_ISLNK(info.st_mode):
        return dict(common, kind='symlink', target=os.readlink(path))
    raise RuntimeError('Refusing a directory or special file in the repair set: ' + str(path))


class StartupRepair:
    def __init__(self, installer):
        self.base = installer
        self.prefix = installer.path(BASE.PREFIX)
        self.directory = installer.state_dir / RECORD
        self.record_path = self.directory / 'record.json'

    def save_record(self, record):
        self.base.write(BASE.STATE + '/' + RECORD + '/record.json',
                        json.dumps(record, indent=2) + '\n', mode=0o600)

    def load_record(self):
        return json.loads(self.record_path.read_text())

    def anchor(self):
        path = self.base.package / ANCHOR
        if path.is_symlink() or not path.is_file():
            raise RuntimeError('Missing regular old-manifest anchor: ' + ANCHOR)
        return path.read_bytes(), json.loads(path.read_text()), BASE.digest(path)

    def check_scope(self, old, new):
        before, after = entries(old), entries(new)
        changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
        if not changed:
            raise RuntimeError('The package contains no startup repair changes')
        if any(not allowed(path) for path in changed):
            raise RuntimeError('Repair attempts to modify files outside session_health.py and build/grd-prefix')
        if HEALTH not in new['files']:
            raise RuntimeError('The readiness helper must remain a regular file')
        # Keep changed links inside GRD, not merely somewhere in the whole
        # application. They must never redirect writes into Mutter or secrets.
        for relative in changed:
            item = after.get(relative)
            if item and item['kind'] == 'symlink':
                source = self.base.package / 'payload' / relative
                grd_root = self.base.package / 'payload/build/grd-prefix'
                if not source.resolve().is_relative_to(grd_root.resolve()):
                    raise RuntimeError('Changed GRD symlink escapes its resource tree: ' + relative)
        return changed

    def process_identity(self, pid):
        try:
            status = dict(line.split(':', 1) for line in self.base.path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
            fields = self.base.path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()
            if status['State'].split()[0] in ('Z', 'X', 'x') or int(status.get('CoreDumping', '0')):
                raise RuntimeError('Observed process is exiting or dumping core')
            return {'pid': pid, 'start_time': int(fields[19])}
        except (OSError, ValueError, IndexError, KeyError) as error:
            raise RuntimeError(f'Cannot confirm running process {pid}') from error

    def runtime_snapshot(self):
        snapshot = {}
        for key, name, user in (
            ('gnome', 'org.gnome.Shell@ubuntu.service', True),
            ('supervisor', 'ubuntu-native-takeover.service', True),
            ('gateway', 'ubuntu-native-login.service', False),
        ):
            properties = self.base.unit_properties(name, user=user)
            pid = int(properties.get('MainPID', '0'))
            if properties.get('ActiveState') != 'active' or pid <= 0:
                raise RuntimeError('Expected running service is not active: ' + name)
            snapshot[key] = self.process_identity(pid)
        supervisor = snapshot['supervisor']['pid']
        children = self.base.path(f'/proc/{supervisor}/task/{supervisor}/children').read_text().split()
        candidates = []
        for child in children:
            command = self.base.path(f'/proc/{child}/cmdline').read_bytes().split(b'\0')[0]
            if command.endswith(b'/gnome-remote-desktop-daemon'):
                candidates.append(int(child))
        if len(candidates) != 1:
            raise RuntimeError('Cannot identify the single running native GRD child')
        snapshot['grd'] = self.process_identity(candidates[0])
        return snapshot

    def stage(self, manifest):
        # Stage on the /opt filesystem so publishing each entry is an atomic
        # rename, including executable files currently mapped by a live GRD.
        directory = Path(tempfile.mkdtemp(prefix='.native-grd-startup-', dir=self.prefix.parent))
        try:
            for relative, item in entries(manifest).items():
                if not allowed(relative):
                    continue
                source = self.base.package / 'payload' / relative
                target = directory / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if item['kind'] == 'file':
                    shutil.copyfile(source, target)
                    target.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)
                    if BASE.digest(target) != item['sha256']:
                        raise RuntimeError('Staged resource checksum mismatch: ' + relative)
                else:
                    target.symlink_to(item['target'])
                os.chown(target, self.base.owner_uid, self.base.owner_gid, follow_symlinks=False)
            for current, directories, _ in os.walk(directory, followlinks=False):
                for path in [Path(current), *(Path(current) / name for name in directories)]:
                    if not path.is_symlink():
                        path.chmod(0o755)
                        os.chown(path, self.base.owner_uid, self.base.owner_gid)
            return directory
        except Exception:
            shutil.rmtree(directory)
            raise

    def prepare_record(self, state, old_bytes, old_hash, new, new_hash, changed, staged, before_runtime):
        self.base.ensure_directory(self.directory, mode=0o700)
        os.chown(self.directory, self.base.owner_uid, self.base.owner_gid)
        self.directory.chmod(0o700)
        backups = self.directory / 'files'
        self.base.ensure_directory(backups, mode=0o700)
        self.base.write(BASE.STATE + '/' + RECORD + '/before-state.json',
                        self.base.state_file.read_bytes(), mode=0o600)
        self.base.write(BASE.STATE + '/' + RECORD + '/before-manifest.json', old_bytes, mode=0o600)
        record = {'version': 1, 'status': 'prepared', 'before_bundle': old_hash,
                  'after_bundle': new_hash, 'before_manifest': state['payload_manifest'],
                  'after_manifest': new, 'changed': changed, 'before': {}, 'after': {},
                  'before_state': BASE.metadata(self.directory / 'before-state.json'),
                  'runtime_before': before_runtime}
        after_entries = entries(new)
        for index, relative in enumerate(changed):
            target = self.prefix / relative
            self.base.no_symlink_path(target.parent)
            before = fingerprint(target)
            if before['kind'] == 'file':
                backup = backups / str(index)
                shutil.copyfile(target, backup)
                backup.chmod(0o600)
                os.chown(backup, self.base.owner_uid, self.base.owner_gid)
                if BASE.digest(backup) != before['sha256']:
                    raise RuntimeError('File changed while being backed up: ' + relative)
                before['backup'] = 'files/' + str(index)
            record['before'][relative] = before
            record['after'][relative] = fingerprint(staged / relative) if relative in after_entries else {'kind': 'absent'}
        self.save_record(record)
        return record

    def check_files(self, record, side):
        for relative in record['changed']:
            expected = {key: value for key, value in record[side][relative].items() if key != 'backup'}
            if fingerprint(self.prefix / relative) != expected:
                raise RuntimeError('Repair file changed; refusing to overwrite: ' + relative)

    def verify_backups(self, record):
        before_state = self.directory / 'before-state.json'
        if BASE.metadata(before_state) != record['before_state']:
            raise RuntimeError('Original state backup changed')
        if BASE.digest(self.directory / 'before-manifest.json') != record['before_bundle']:
            raise RuntimeError('Original manifest backup changed')
        for relative, item in record['before'].items():
            if item['kind'] == 'file':
                backup = self.directory / item['backup']
                if backup.is_symlink() or not backup.is_file() or BASE.digest(backup) != item['sha256']:
                    raise RuntimeError('Repair backup checksum mismatch: ' + relative)

    def replace_entry(self, relative, source, expected):
        target = self.prefix / relative
        self.base.no_symlink_path(target.parent)
        self.base.ensure_directory(target.parent)
        if fingerprint(target)['kind'] not in ('absent', 'file', 'symlink'):
            raise RuntimeError('Unsupported repair destination: ' + relative)
        if expected['kind'] == 'absent':
            target.unlink(missing_ok=True)
        else:
            os.replace(source, target)  # Never truncate a live executable inode.

    def restore_files(self, record):
        self.verify_backups(record)
        # Recreate each original in its destination filesystem, then rename.
        # Resources precede executables; deleting newly introduced entries is last.
        def order(relative):
            item = record['before'][relative]
            return (2 if item['kind'] == 'absent' else 1 if item.get('mode', 0) & 0o111 else 0, relative)
        for relative in sorted(record['changed'], key=order):
            item = record['before'][relative]
            target = self.prefix / relative
            self.base.no_symlink_path(target.parent)
            self.base.ensure_directory(target.parent)
            temporary = None
            try:
                if item['kind'] == 'file':
                    fd, name = tempfile.mkstemp(prefix='.restore-grd-', dir=target.parent)
                    temporary = Path(name)
                    with os.fdopen(fd, 'wb') as output, (self.directory / item['backup']).open('rb') as source:
                        shutil.copyfileobj(source, output)
                        output.flush()
                        os.fsync(output.fileno())
                        os.fchown(output.fileno(), item['uid'], item['gid'])
                        os.fchmod(output.fileno(), item['mode'])
                elif item['kind'] == 'symlink':
                    fd, name = tempfile.mkstemp(prefix='.restore-grd-', dir=target.parent)
                    os.close(fd)
                    temporary = Path(name)
                    temporary.unlink()
                    temporary.symlink_to(item['target'])
                    os.chown(temporary, item['uid'], item['gid'], follow_symlinks=False)
                self.replace_entry(relative, temporary, item)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def apply(self):
        new, new_hash = self.base.verify_bundle()
        old_bytes, old, old_hash = self.anchor()
        state = self.base.load()
        if self.record_path.exists():
            record = self.load_record()
            if record['before_bundle'] != old_hash or record['after_bundle'] != new_hash:
                raise RuntimeError('Existing startup repair record belongs to a different package')
            if record['status'] == 'applied':
                if state['bundle_sha256'] != new_hash or state['payload_manifest'] != new:
                    raise RuntimeError('Installed repair state no longer matches this package')
                self.base.assert_current(state)
                self.check_files(record, 'after')
                return {'status': 'applied', 'idempotent': True, 'services_changed': False}
            if record['status'] not in ('rolled-back', 'apply-failed-restored'):
                raise RuntimeError('An interrupted startup repair requires rollback before retrying')
            self.verify_backups(record)
            self.check_files(record, 'before')
        if state['phase'] != 'active' or not state.get('gateway_active'):
            raise RuntimeError('Startup repair requires the established active installation record')
        if old_hash != state['bundle_sha256'] or old != state['payload_manifest']:
            raise RuntimeError('Old manifest anchor does not exactly match the installed state')
        changed = self.check_scope(old, new)
        self.base.assert_current(state)
        before_runtime = self.runtime_snapshot()
        staged = self.stage(new)
        try:
            record = self.prepare_record(state, old_bytes, old_hash, new, new_hash, changed, staged, before_runtime)
            # Recheck after staging/backups, before touching any installed entry.
            self.base.assert_current(state)
            self.check_files(record, 'before')
            record['status'] = 'applying'
            self.save_record(record)
            try:
                def order(relative):
                    item = record['after'][relative]
                    return (2 if item['kind'] == 'absent' else 1 if item.get('mode', 0) & 0o111 else 0, relative)
                for relative in sorted(changed, key=order):
                    self.replace_entry(relative, staged / relative, record['after'][relative])
                self.base.verify_prefix(new)
                self.check_files(record, 'after')
                after_runtime = self.runtime_snapshot()
                if after_runtime != before_runtime:
                    raise RuntimeError('Observed desktop/backend process identity changed during repair')
                updated = copy.deepcopy(state)
                updated.update(payload_manifest=new, bundle_sha256=new_hash)
                self.base.save(updated)
                record.update(status='applied', runtime_after=after_runtime)
                self.save_record(record)
            except Exception as failure:
                try:
                    self.restore_files(record)
                    self.base.write(BASE.STATE + '/state.json',
                                    (self.directory / 'before-state.json').read_bytes(), mode=0o600)
                    record.update(status='apply-failed-restored', error=str(failure))
                    self.save_record(record)
                except Exception as recovery_failure:
                    record.update(status='recovery-required', error=str(failure),
                                  recovery_error=str(recovery_failure))
                    with contextlib.suppress(Exception):
                        self.save_record(record)
                    raise RuntimeError('Startup repair failed and automatic restoration failed; resolve the error and run rollback: ' + str(recovery_failure)) from failure
                raise RuntimeError('Startup repair failed; original files and state were restored: ' + str(failure)) from failure
        finally:
            shutil.rmtree(staged)
        return {'status': 'applied', 'updated_paths': changed, 'services_changed': False,
                'running_processes_unchanged': before_runtime, 'bundle_sha256': new_hash}

    def rollback(self):
        record = self.load_record()
        state = self.base.load()
        self.verify_backups(record)
        if record['status'] in ('rolled-back', 'apply-failed-restored'):
            if state['bundle_sha256'] != record['before_bundle'] or state['payload_manifest'] != record['before_manifest']:
                raise RuntimeError('Restored state has subsequently changed')
            self.base.assert_current(state)
            self.check_files(record, 'before')
            return {'status': 'rolled-back', 'idempotent': True, 'services_changed': False}
        if record['status'] not in ('applied', 'applying', 'prepared', 'recovery-required', 'rolling-back'):
            raise RuntimeError('Unknown startup repair state')
        if state['bundle_sha256'] not in (record['before_bundle'], record['after_bundle']):
            raise RuntimeError('Another package now owns the installation state')
        expected_manifest = record['after_manifest'] if state['bundle_sha256'] == record['after_bundle'] else record['before_manifest']
        if state['payload_manifest'] != expected_manifest:
            raise RuntimeError('Installed manifest does not match the recorded bundle')
        if any(not allowed(relative) for relative in record['changed']):
            raise RuntimeError('Repair record contains an out-of-scope path')
        # Interrupted transactions may have a mix of old/new files. Accept only
        # those exact versions; never overwrite a third party's later edits.
        for relative in record['changed']:
            actual = fingerprint(self.prefix / relative)
            permitted = [{key: value for key, value in record[side][relative].items() if key != 'backup'}
                         for side in ('before', 'after')]
            if actual not in permitted:
                raise RuntimeError('Repair file was subsequently modified: ' + relative)
        unchanged = copy.deepcopy(state)
        unchanged['payload_manifest'] = copy.deepcopy(record['before_manifest'])
        for group in ('files', 'symlinks'):
            for relative in record['changed']:
                unchanged['payload_manifest'].get(group, {}).pop(relative, None)
        self.base.assert_current(unchanged)
        before_runtime = self.runtime_snapshot()
        record['status'] = 'rolling-back'
        self.save_record(record)
        self.restore_files(record)
        updated = copy.deepcopy(state)
        updated.update(payload_manifest=record['before_manifest'], bundle_sha256=record['before_bundle'])
        self.base.save(updated)
        self.base.assert_current(updated)
        self.check_files(record, 'before')
        after_runtime = self.runtime_snapshot()
        if after_runtime != before_runtime:
            raise RuntimeError('Files and manifest were restored, but observed process identity changed; no service action was performed')
        record.update(status='rolled-back', rollback_runtime=after_runtime)
        self.save_record(record)
        return {'status': 'rolled-back', 'services_changed': False,
                'running_processes_unchanged': after_runtime, 'bundle_sha256': record['before_bundle']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('apply', 'rollback'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run this file with sudo; it never changes service state.')
    installer = BASE.Installer(HERE)
    with installer.lock():
        result = getattr(StartupRepair(installer), args.command)()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
