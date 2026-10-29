#!/usr/bin/python3
"""Publish the tested next-boot runtime atomically, without any service action.

Linux RENAME_EXCHANGE preserves the complete previous runtime and every live
executable mapping. Configuration is backed up separately. No daemon-reload,
restart, session operation, password or credential modification is performed.
"""
import argparse
import copy
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('native_base', HERE / 'install-greeter-fix.py')
BASE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(BASE)
RECORD = 'handover-fix'
SHELL_UNIT = '/etc/systemd/user/org.gnome.Shell@.service.d/80-native-login.conf'
GUARD_UNIT = '/etc/systemd/user/ubuntu-native-login-guard.service'
ALLOWED_UNITS = {SHELL_UNIT, GUARD_UNIT}
ALLOWED_CODE = {'scripts/native-session.py', 'scripts/finish-login-guard.py',
                'takeover/startup_guard.py', 'takeover/login_gateway.py',
                'takeover/native_broker.py', 'scripts/check-native-health.py'}


def exchange(first, second):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(first), -100, os.fsencode(second), 2):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def file_record(path):
    if not path.exists() and not path.is_symlink():
        return {'exists': False}
    return dict(exists=True, **BASE.metadata(path))


class HandoverRepair:
    def __init__(self, base):
        self.base = base
        self.prefix = base.path(BASE.PREFIX)
        self.directory = base.state_dir / RECORD
        self.record_path = self.directory / 'record.json'

    def save(self, record):
        self.base.write(BASE.STATE + '/' + RECORD + '/record.json',
                        json.dumps(record, indent=2) + '\n', mode=0o600)

    def verify_tree(self, tree, manifest):
        self.base.no_symlink_path(tree)
        for relative, checksum in manifest['files'].items():
            path = tree / BASE.checked_relative(relative)
            if path.is_symlink() or not path.is_file() or BASE.digest(path) != checksum:
                raise RuntimeError('Runtime checksum changed: ' + relative)
        for relative, target in manifest.get('symlinks', {}).items():
            path = tree / BASE.checked_relative(relative)
            if not path.is_symlink() or os.readlink(path) != target:
                raise RuntimeError('Runtime link changed: ' + relative)
        for current, directories, files in os.walk(tree, followlinks=False):
            for path in [Path(current), *(Path(current) / n for n in directories + files)]:
                info = path.lstat()
                if info.st_uid != self.base.owner_uid or (not path.is_symlink() and info.st_mode & 0o022):
                    raise RuntimeError('Runtime is not exclusively installer-owned: ' + str(path))

    def snapshot(self, require_complete=True):
        result = {}
        for name, user in (('org.gnome.Shell@ubuntu.service', True),
                           ('ubuntu-native-takeover.service', True),
                           ('ubuntu-native-login.service', False)):
            props = self.base.unit_properties(name, user=user)
            pid = int(props.get('MainPID', '0'))
            if pid <= 0 or props.get('ActiveState') != 'active':
                if require_complete:
                    raise RuntimeError('Expected live service is not active: ' + name)
                result[name] = {'pid': pid, 'active_state': props.get('ActiveState')}
                continue
            fields = self.base.path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()
            if fields[0] in ('Z', 'X', 'x'):
                raise RuntimeError('Expected live process is exiting: ' + name)
            result[name] = {'pid': pid, 'start_time': int(fields[19])}
        supervisor = result['ubuntu-native-takeover.service']['pid']
        children_path = self.base.path(f'/proc/{supervisor}/task/{supervisor}/children')
        children = children_path.read_text().split() if supervisor > 0 and children_path.exists() else []
        for pid in children:
            try:
                command = self.base.path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')[0]
                if not command.endswith(b'/gnome-remote-desktop-daemon'):
                    continue
                fields = self.base.path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()
                result['native-grd'] = {'pid': int(pid), 'start_time': int(fields[19])}
            except FileNotFoundError:
                continue
        if 'native-grd' not in result and require_complete:
            raise RuntimeError('Current native RDP child is missing')
        return result

    def inputs(self):
        new, checksum = self.base.verify_bundle()
        anchor = self.base.package / 'before-handover-manifest.json'
        if anchor.is_symlink() or not anchor.is_file():
            raise RuntimeError('Missing original manifest anchor')
        old = json.loads(anchor.read_text())
        def flat(m):
            return {**m['files'], **m.get('symlinks', {})}
        before, after = flat(old), flat(new)
        changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
        if not changed or any(p not in ALLOWED_CODE and not p.startswith('build/native-prefix/') for p in changed):
            raise RuntimeError('Out-of-scope or empty handover update')
        # The PAM binary is unchanged; do not accidentally replace the local
        # authentication mechanism while repairing compositor state handling.
        helper = 'build/native-prefix/libexec/native-local-auth'
        if before.get(helper) != after.get(helper):
            raise RuntimeError('Local PAM helper must remain unchanged')
        units = json.loads((self.base.package / 'units.json').read_text())
        if set(units) != ALLOWED_UNITS:
            raise RuntimeError('Unexpected unit modification set')
        for absolute, item in units.items():
            source = self.base.package / BASE.checked_relative(item['source'])
            if source.is_symlink() or not source.is_file():
                raise RuntimeError('Unit checksum mismatch: ' + absolute)
            item['data'] = source.read_bytes()
            if hashlib.sha256(item['data']).hexdigest() != item['sha256']:
                raise RuntimeError('Unit checksum mismatch: ' + absolute)
        return old, BASE.digest(anchor), new, checksum, changed, units

    def stage(self, manifest):
        staged = Path(tempfile.mkdtemp(prefix='.native-handover-', dir=self.prefix.parent))
        try:
            shutil.copytree(self.base.package / 'payload', staged, symlinks=True, dirs_exist_ok=True)
            for current, directories, files in os.walk(staged, followlinks=False):
                for path in [Path(current), *(Path(current) / n for n in directories + files)]:
                    os.chown(path, self.base.owner_uid, self.base.owner_gid, follow_symlinks=False)
                    if not path.is_symlink():
                        path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
            self.verify_tree(staged, manifest)
            return staged
        except Exception:
            shutil.rmtree(staged)
            raise

    def configuration_matches(self, record, side):
        for absolute, versions in record['configuration'].items():
            actual = file_record(self.base.path(absolute))
            expected = {k: v for k, v in versions[side].items() if k != 'backup'}
            if actual != expected:
                raise RuntimeError('Configuration changed: ' + absolute)

    def restore(self, record):
        # Validate every backup before publishing the first restored entry.
        state_backup = self.directory / 'before-state.json'
        if BASE.digest(state_backup) != record['before_state_sha256']:
            raise RuntimeError('Installation-state backup changed')
        for absolute, versions in record['configuration'].items():
            actual = file_record(self.base.path(absolute))
            allowed = [{k: v for k, v in versions[s].items() if k != 'backup'} for s in ('before', 'after')]
            if actual not in allowed:
                raise RuntimeError('Later configuration edit prevents rollback: ' + absolute)
            before = versions['before']
            if before['exists'] and BASE.digest(self.base.state_dir / before['backup']) != before['sha256']:
                raise RuntimeError('Configuration backup changed: ' + absolute)
        previous = Path(record['exchange_directory'])
        if previous.parent != self.prefix.parent or not previous.name.startswith('.native-handover-'):
            raise RuntimeError('Unsafe exchange directory in record')
        identity = self.prefix.stat().st_ino
        if identity == record['new_inode']:
            self.verify_tree(self.prefix, record['after_manifest'])
            self.verify_tree(previous, record['before_manifest'])
            exchange(self.prefix, previous)
        elif identity == record['old_inode']:
            self.verify_tree(self.prefix, record['before_manifest'])
        else:
            raise RuntimeError('Unknown runtime directory prevents rollback')
        for absolute, versions in record['configuration'].items():
            self.base.restore_file(absolute, versions['before'])
        self.base.write(BASE.STATE + '/state.json', state_backup.read_bytes(), mode=0o600)
        record['status'] = 'rolled-back'
        self.save(record)

    def apply(self):
        old, old_hash, new, new_hash, changed, units = self.inputs()
        state = self.base.load()
        if self.record_path.exists():
            record = json.loads(self.record_path.read_text())
            if record['after_bundle'] != new_hash or record['before_bundle'] != old_hash:
                raise RuntimeError('Existing handover transaction belongs to another build')
            if record['status'] == 'applied':
                if state['bundle_sha256'] != new_hash:
                    raise RuntimeError('Another installation superseded this repair')
                self.base.assert_current(state)
                self.configuration_matches(record, 'after')
                return {'status': 'applied', 'idempotent': True, 'services_changed': False}
            raise RuntimeError('Existing transaction must be inspected; use rollback if interrupted')
        if state['bundle_sha256'] != old_hash or state['payload_manifest'] != old:
            raise RuntimeError('Original manifest does not exactly match the installed state')
        if state.get('phase') != 'active' or not state.get('gateway_active'):
            raise RuntimeError('Expected established active installation')
        self.base.assert_current(state)
        before_runtime = self.snapshot()
        self.base.ensure_directory(self.directory, mode=0o700)
        state_backup = BASE.STATE + '/' + RECORD + '/before-state.json'
        self.base.write(state_backup, self.base.state_file.read_bytes(), mode=0o600)
        staged = self.stage(new)
        record = {'version': 1, 'status': 'prepared', 'before_bundle': old_hash,
                  'after_bundle': new_hash, 'before_manifest': old, 'after_manifest': new,
                  'changed': changed, 'exchange_directory': str(staged),
                  'old_inode': self.prefix.stat().st_ino, 'new_inode': staged.stat().st_ino,
                  'before_state_sha256': BASE.digest(self.base.path(state_backup)),
                  'configuration': {}, 'runtime_before': before_runtime}
        for index, (absolute, item) in enumerate(sorted(units.items())):
            self.base.no_symlink_path(self.base.path(absolute))
            before = file_record(self.base.path(absolute))
            if before['exists']:
                before['backup'] = RECORD + '/unit-' + str(index)
                self.base.write(BASE.STATE + '/' + before['backup'], self.base.path(absolute).read_bytes(), mode=0o600)
            record['configuration'][absolute] = {'before': before, 'after': {
                'exists': True, 'sha256': item['sha256'], 'uid': self.base.owner_uid,
                'gid': self.base.owner_gid, 'mode': 0o644}}
        self.save(record)
        try:
            self.base.assert_current(state)
            self.configuration_matches(record, 'before')
            exchange(self.prefix, staged)
            record['status'] = 'runtime-published'
            self.save(record)
            updated = copy.deepcopy(state)
            for absolute, item in sorted(units.items()):
                self.base.write(absolute, item['data'])
                updated['installed'][absolute] = BASE.metadata(self.base.path(absolute))
                updated['original'].setdefault(absolute, record['configuration'][absolute]['before'])
            updated.update(bundle_sha256=new_hash, payload_manifest=new)
            self.configuration_matches(record, 'after')
            self.base.assert_current(updated)
            after_runtime = self.snapshot()
            if after_runtime != before_runtime:
                raise RuntimeError('A protected process identity changed during publication')
            self.base.save(updated)
            record.update(status='applied', runtime_after=after_runtime)
            self.save(record)
        except Exception as error:
            try:
                self.restore(record)
            except Exception as restore_error:
                raise RuntimeError('Publication failed; rollback requires attention: ' + str(restore_error)) from error
            raise RuntimeError('Publication failed; previous runtime/configuration restored: ' + str(error)) from error
        return {'status': 'applied', 'services_changed': False, 'next_boot_required': True,
                'changed': changed, 'bundle_sha256': new_hash,
                'running_processes_unchanged': before_runtime, 'backup_runtime': str(staged)}

    def rollback(self):
        record = json.loads(self.record_path.read_text())
        current = self.base.load()
        if current['bundle_sha256'] not in (record['before_bundle'], record['after_bundle']):
            raise RuntimeError('Another installation superseded this repair')
        if record['status'] == 'rolled-back':
            self.base.assert_current(current)
            return {'status': 'rolled-back', 'idempotent': True, 'services_changed': False}
        # Rollback must also work after the very boot failure it is meant to
        # recover from, where a desktop backend may not have started at all.
        before_runtime = self.snapshot(require_complete=False)
        self.restore(record)
        if self.snapshot(require_complete=False) != before_runtime:
            raise RuntimeError('Rollback complete, but protected processes changed independently')
        return {'status': 'rolled-back', 'services_changed': False, 'next_boot_required': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('apply', 'rollback'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Use sudo for atomic installation; no service is restarted.')
    base = BASE.Installer(HERE)
    with base.lock():
        print(json.dumps(getattr(HandoverRepair(base), args.command)(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
