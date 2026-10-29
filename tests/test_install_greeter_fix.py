import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('install_greeter_fix', ROOT / 'scripts/install-greeter-fix.py')
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'root'
        self.package = Path(self.temporary.name) / 'package'
        self.package.mkdir()
        self.calls = []
        self.gateway_invocation = 0
        self.account = SimpleNamespace(pw_name='desktop', pw_uid=max(1000, os.getuid()),
                                       pw_gid=os.getgid(), pw_dir='/home/desktop')
        self.original_gateway = ('[Service]\nWorkingDirectory=/opt/ubuntu-native-takeover\n'
                                 'ExecStart=/usr/bin/python3 -m takeover.login_gateway\nRestart=on-failure\n')
        self.put('/etc/ubuntu-native-login/config.json', '{"user":"desktop"}')
        self.put(INSTALL.GATEWAY, self.original_gateway)
        self.put('/etc/systemd/user/org.gnome.Shell@.service.d/80-native-login.conf', 'original system shell\n', 0o640)
        self.put('/etc/systemd/user/ubuntu-native-greeter.service', 'ConditionUser=gdm\n')
        self.put('/home/desktop/.config/systemd/user/org.gnome.Shell@ubuntu.service.d/90-native-takeover.conf', 'original user shell\n')
        self.put('/home/desktop/.config/systemd/user/ubuntu-native-takeover.service.d/95-system-login.conf', 'original backend\n')
        for relative in INSTALL.SECRETS:
            self.put('/home/desktop/.local/share/ubuntu-native-takeover/' + relative,
                     'synthetic test credential\n', 0o600)
        self.put(f'/run/user/{self.account.pw_uid}/bus', '')
        self.put('/usr/lib/test-abi.so', 'current ABI\n')
        payload = self.package / 'payload'
        (payload / 'takeover').mkdir(parents=True)
        shutil.copyfile(ROOT / 'takeover/greeter_config.py', payload / 'takeover/greeter_config.py')
        shutil.copyfile(ROOT / 'takeover/startup_guard.py', payload / 'takeover/startup_guard.py')
        (payload / 'takeover/login_gateway.py').write_text('# initial gateway\n')
        (payload / 'run.py').write_text('print("candidate")\n')
        (payload / 'run.py').chmod(0o775)
        (payload / 'alias.py').symlink_to('run.py')
        self.write_manifest()
        (self.package / 'system-abi.json').write_text(json.dumps({
            '/usr/lib/test-abi.so': INSTALL.digest(self.root / 'usr/lib/test-abi.so')}))
        self.installer = INSTALL.Installer(self.package, root=self.root,
            runner=self.run_command, account_lookup=lambda name: self.account,
            owner_uid=os.getuid(), owner_gid=os.getgid(), gdm_gid=os.getgid())

    def put(self, absolute, text, mode=0o644):
        path = self.root / absolute.lstrip('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)
        return path

    def write_manifest(self):
        payload = self.package / 'payload'
        files, links = {}, {}
        for path in payload.rglob('*'):
            if path.is_symlink():
                links[path.relative_to(payload).as_posix()] = os.readlink(path)
            elif path.is_file():
                files[path.relative_to(payload).as_posix()] = INSTALL.digest(path)
        (self.package / 'payload-manifest.json').write_text(json.dumps({
            'version': 1, 'files': files, 'symlinks': links}))

    def run_command(self, arguments, **kwargs):
        self.calls.append(arguments)
        if arguments[:2] == ['dconf', 'compile']:
            Path(arguments[2]).write_bytes(b'compiled synthetic settings\x00')
        if arguments == ['systemctl', '--no-block', 'restart', 'ubuntu-native-login.service']:
            self.gateway_invocation += 1
        if arguments[:3] == ['systemctl', 'show', 'ubuntu-native-login.service']:
            active = bool(self.gateway_invocation)
            timeout = '10s' if 'TimeoutStopSec=10s' in self.installer.path(INSTALL.GATEWAY).read_text() else '1min 30s'
            output = (f'MainPID={40 + self.gateway_invocation if active else 0}\n'
                      f'ActiveState={"active" if active else "inactive"}\n'
                      f'SubState={"running" if active else "dead"}\nJob=0\n'
                      f'InvocationID={self.gateway_invocation}\nResult=success\nTimeoutStopUSec={timeout}\n')
            return subprocess.CompletedProcess(arguments, 0, output, '')
        return subprocess.CompletedProcess(arguments, 0, '', '')

    def install(self):
        return self.installer.install()

    def test_plan_has_no_mutations_or_commands(self):
        result = self.installer.plan()
        self.assertFalse(result['install_restarts_services'])
        self.assertEqual(self.calls, [])
        self.assertFalse(self.installer.state_dir.exists())
        self.assertFalse(self.installer.path(INSTALL.PREFIX).exists())

    def test_install_only_reloads_definitions_and_sets_secret_permissions(self):
        self.install()
        self.assertFalse(any('restart' in call or 'stop' in call or 'start' in call for call in self.calls))
        self.assertIn(['systemctl', 'daemon-reload'], self.calls)
        self.assertTrue(any('runuser' == call[0] and call[-3:] == ['systemctl', '--user', 'daemon-reload'] for call in self.calls))
        unit = self.installer.path('/etc/systemd/user/ubuntu-native-greeter.service').read_text()
        self.assertIn('ConditionGroup=gdm', unit)
        self.assertNotIn('ConditionUser=', unit)
        self.assertIn('WatchdogSec=60', unit)
        shell = self.installer.path('/etc/systemd/user/org.gnome.Shell@.service.d/80-native-login.conf').read_text()
        self.assertIn('ExecStartPost=-/usr/bin/systemctl --user --no-block start ubuntu-native-login-guard.service', shell)
        self.assertNotIn('/scripts/finish-login-guard.py', shell)
        guard = self.installer.path('/etc/systemd/user/ubuntu-native-login-guard.service').read_text()
        self.assertIn('After=org.gnome.Shell@ubuntu.service graphical-session.target', guard)
        self.assertIn('Restart=no', guard)
        self.assertNotIn('Before=', guard)
        for relative in INSTALL.SECRETS:
            info = self.installer.path(INSTALL.GREETER_DATA + '/' + relative).stat()
            self.assertEqual(info.st_mode & 0o777, 0o640)
            self.assertEqual(info.st_gid, os.getgid())
        self.assertEqual(self.installer.path(INSTALL.GREETER_DATA).stat().st_mode & 0o777, 0o750)
        self.assertEqual(self.installer.path(INSTALL.GREETER_CONFIG + '/dconf-profile').read_text(),
                         'file-db:/etc/ubuntu-native-login/greeter/settings.db\n')
        self.assertNotIn('synthetic test credential', self.installer.state_file.read_text())
        self.assertEqual(self.installer.path(INSTALL.PREFIX + '/run.py').stat().st_mode & 0o777, 0o755)

    def test_repeat_install_is_idempotent_without_recompile_or_reload(self):
        self.install()
        self.calls.clear()
        self.assertTrue(self.install()['idempotent'])
        self.assertEqual(self.calls, [])
        self.installer.verify_bundle()  # Config loading must not create __pycache__ in payload.

    def test_modified_payload_and_abi_are_rejected_before_any_installation(self):
        candidate = self.package / 'payload/run.py'
        original = candidate.read_bytes()
        candidate.write_text('modified\n')
        with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
            self.install()
        self.assertEqual(self.calls, [])
        candidate.write_bytes(original)
        self.put('/usr/lib/test-abi.so', 'updated ABI\n')
        with self.assertRaisesRegex(RuntimeError, 'System ABI changed'):
            self.install()
        self.assertEqual(self.installer.path(INSTALL.GATEWAY).read_text(), self.original_gateway)

    def test_extra_payload_file_and_escaping_symlink_are_rejected(self):
        extra = self.package / 'payload/extra'
        extra.write_text('extra')
        with self.assertRaisesRegex(RuntimeError, 'inventory'):
            self.install()
        extra.unlink()
        link = self.package / 'payload/alias.py'
        link.unlink()
        link.symlink_to('../system-abi.json')
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeError, 'escapes'):
            self.install()

    def test_unmanaged_candidate_is_never_overwritten(self):
        self.installer.path(INSTALL.PREFIX).mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            self.install()
        self.assertEqual(self.installer.path(INSTALL.GATEWAY).read_text(), self.original_gateway)

    def test_unknown_dedicated_greeter_directories_are_never_modified(self):
        for absolute in (INSTALL.GREETER_CONFIG, INSTALL.GREETER_DATA):
            with self.subTest(directory=absolute):
                directory = self.installer.path(absolute)
                directory.mkdir(parents=True)
                directory.chmod(0o700)
                existing = directory / 'existing-admin-data'
                existing.write_text('unrelated configuration')
                with self.assertRaisesRegex(RuntimeError, 'without this version record'):
                    self.install()
                self.assertEqual(existing.read_text(), 'unrelated configuration')
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                self.assertEqual(self.calls, [])
                shutil.rmtree(directory)

    def test_changed_installed_configuration_blocks_repeat_install_and_rollback(self):
        self.install()
        gateway = self.installer.path(INSTALL.GATEWAY)
        gateway.write_text('administrator edit\n')
        for operation in (self.install, self.installer.rollback):
            with self.assertRaisesRegex(RuntimeError, 'configuration was changed'):
                operation()
        self.assertEqual(gateway.read_text(), 'administrator edit\n')

    def test_rollback_restores_bytes_and_mode_without_restarting_unactivated_gateway(self):
        original = INSTALL.metadata(self.installer.path('/etc/systemd/user/org.gnome.Shell@.service.d/80-native-login.conf'))
        self.install()
        self.calls.clear()
        result = self.installer.rollback()
        self.assertFalse(result['gateway_restarted'])
        self.assertEqual(self.installer.path(INSTALL.GATEWAY).read_text(), self.original_gateway)
        self.assertEqual(INSTALL.metadata(self.installer.path('/etc/systemd/user/org.gnome.Shell@.service.d/80-native-login.conf')), original)
        self.assertFalse(self.installer.path(INSTALL.GREETER_CONFIG + '/settings.db').exists())
        self.assertTrue(self.installer.path(INSTALL.GREETER_DATA + '/tls.key').exists())
        self.assertTrue(self.installer.path(INSTALL.PREFIX).exists())
        self.assertFalse(any('restart' in call or 'stop' in call for call in self.calls))

    def test_corrupt_backup_blocks_restore_before_changing_any_unit(self):
        self.install()
        state = self.installer.load()
        last = [record for record in state['original'].values() if record['exists']][-1]
        (self.installer.state_dir / last['backup']).write_text('damaged backup')
        before = self.installer.path(INSTALL.GATEWAY).read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'backup checksum'):
            self.installer.rollback()
        self.assertEqual(self.installer.path(INSTALL.GATEWAY).read_bytes(), before)

    def test_install_reload_failure_restores_units_without_restarting(self):
        original_run = self.run_command
        def failure(arguments, **kwargs):
            if arguments == ['systemctl', 'daemon-reload']:
                self.calls.append(arguments)
                return subprocess.CompletedProcess(arguments, 1, '', 'synthetic reload failure')
            return original_run(arguments, **kwargs)
        self.installer.runner = failure
        with self.assertRaisesRegex(RuntimeError, 'synthetic reload failure'):
            self.install()
        self.assertEqual(self.installer.path(INSTALL.GATEWAY).read_text(), self.original_gateway)
        self.assertEqual(self.installer.load()['phase'], 'install-failed-restored')
        self.assertFalse(any('restart' in call or 'stop' in call for call in self.calls))

    def test_activation_restarts_only_gateway_and_preserves_recorded_processes(self):
        self.install()
        self.calls.clear()
        snapshot = {'gnome_pid': 10, 'supervisor_pid': 20, 'backend_pid': 21, 'session': '/physical'}
        with patch.object(self.installer, 'idle_snapshot', return_value=snapshot), \
             patch.object(self.installer, 'wait_gateway', return_value={'version': 1, 'endpoints': ['desktop']}):
            result = self.installer.activate_gateway()
        self.assertEqual(result['processes_unchanged'], snapshot)
        self.assertEqual([call for call in self.calls if 'restart' in call],
                         [['systemctl', '--no-block', 'restart', 'ubuntu-native-login.service']])
        self.assertTrue(self.installer.load()['gateway_active'])

    def test_nonidle_activation_does_not_restart_anything(self):
        self.install()
        self.calls.clear()
        with patch.object(self.installer, 'idle_snapshot', side_effect=RuntimeError('remote lease')):
            with self.assertRaisesRegex(RuntimeError, 'remote lease'):
                self.installer.activate_gateway()
        self.assertFalse(any('restart' in call or 'daemon-reload' in call for call in self.calls))

    def test_failed_activation_restores_and_restarts_only_original_gateway(self):
        self.install()
        self.calls.clear()
        with patch.object(self.installer, 'idle_snapshot', return_value={'gnome_pid': 10}), \
             patch.object(self.installer, 'wait_gateway', side_effect=RuntimeError('no registration')):
            with self.assertRaisesRegex(RuntimeError, 'original gateway unit was restored'):
                self.installer.activate_gateway()
        self.assertEqual(self.installer.path(INSTALL.GATEWAY).read_text(), self.original_gateway)
        self.assertEqual([call for call in self.calls if 'restart' in call],
                         [['systemctl', '--no-block', 'restart', 'ubuntu-native-login.service']] * 2)
        self.assertEqual(self.installer.load()['phase'], 'gateway-fallback')
        self.installer.assert_current(self.installer.load())

    def test_rollback_defers_restart_if_a_connection_arrives_after_restoration(self):
        self.install()
        state = self.installer.load()
        state.update(phase='active', gateway_active=True)
        self.installer.save(state)
        self.calls.clear()
        with patch.object(self.installer, 'idle_snapshot', side_effect=[{'pid': 10}, RuntimeError('new remote client')]):
            with self.assertRaisesRegex(RuntimeError, 'rerun rollback'):
                self.installer.rollback()
        self.assertEqual(self.installer.load()['phase'], 'rollback-pending-gateway')
        self.assertEqual(self.installer.path(INSTALL.GATEWAY).read_text(), self.original_gateway)
        self.assertFalse(any('restart' in call for call in self.calls))
        with patch.object(self.installer, 'idle_snapshot', return_value={'pid': 10}):
            self.assertTrue(self.installer.rollback()['gateway_restarted'])
        self.assertEqual([call for call in self.calls if 'restart' in call],
                         [['systemctl', '--no-block', 'restart', 'ubuntu-native-login.service']])

    def test_rollback_detects_candidate_started_after_reboot_without_manual_activation(self):
        self.install()
        self.assertFalse(self.installer.load()['gateway_active'])
        self.put('/proc/42/cmdline', '/usr/bin/python3\0-m\0takeover.login_gateway\0')
        self.installer.path('/proc/42/cwd').symlink_to(self.installer.path(INSTALL.PREFIX))
        with patch.object(self.installer, 'unit_properties', return_value={'ActiveState': 'active', 'MainPID': '42'}):
            self.assertTrue(self.installer.running_gateway_is_candidate())
        self.calls.clear()
        with patch.object(self.installer, 'running_gateway_is_candidate', return_value=True):
            with patch.object(self.installer, 'idle_snapshot', return_value={'pid': 10}) as snapshot:
                result = self.installer.rollback()
            self.assertEqual(snapshot.call_count, 2)
        self.assertTrue(result['gateway_restarted'])
        self.assertEqual([call for call in self.calls if 'restart' in call],
                         [['systemctl', '--no-block', 'restart', 'ubuntu-native-login.service']])

    def prepare_gateway_repair(self):
        self.install()
        state = self.installer.load()
        self.installer.restore_file(INSTALL.GATEWAY, state['original'][INSTALL.GATEWAY])
        state['installed'][INSTALL.GATEWAY] = INSTALL.metadata(self.installer.path(INSTALL.GATEWAY))
        state.update(phase='gateway-fallback', gateway_active=False,
                     activation_error='old restart command timed out', fallback_error='old fallback timed out')
        # The installed October 28 bundle predates the stop-timeout safeguard.
        state['desired_gateway'] = self.original_gateway.replace(
            '/opt/ubuntu-native-takeover\n', INSTALL.PREFIX + '\n')
        self.installer.save(state)
        shutil.copy2(self.package / 'payload-manifest.json', self.package / 'previous-payload-manifest.json')
        (self.package / 'payload/takeover/login_gateway.py').write_text('# repaired gateway shutdown\n')
        self.write_manifest()
        self.calls.clear()
        return state

    def test_gateway_repair_updates_only_module_and_is_idempotent(self):
        old = self.prepare_gateway_repair()
        snapshot = {'gnome_pid': 10, 'supervisor_pid': 20, 'backend_pid': 21, 'session': '/physical'}
        status = {'version': 1, 'endpoints': ['desktop']}
        with patch.object(self.installer, 'idle_snapshot', return_value=snapshot), \
             patch.object(self.installer, 'wait_gateway', return_value=status):
            result = self.installer.repair_gateway()
        self.assertEqual(result['gateway_repair'], 'applied')
        state = self.installer.load()
        backup = self.installer.state_dir / state['gateway_repair']['backup']
        self.assertEqual(backup.read_text(), '# initial gateway\n')
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(state['payload_manifest'], self.installer.verify_bundle()[0])
        self.assertEqual(state['bundle_sha256'], self.installer.verify_bundle()[1])
        self.assertEqual(state['original'], old['original'])
        self.assertIn('TimeoutStopSec=10s', state['desired_gateway'])
        self.assertNotIn('fallback_error', state)
        self.installer.assert_current(state)
        self.assertEqual([call for call in self.calls if 'restart' in call],
                         [['systemctl', '--no-block', 'restart', 'ubuntu-native-login.service']])
        self.calls.clear()
        with patch.object(self.installer, 'gateway_status', return_value=status):
            again = self.installer.repair_gateway()
        self.assertTrue(again['idempotent'])
        self.assertEqual(again['gateway_repair'], 'already-applied')
        self.assertEqual(self.calls, [])
        self.assertEqual(backup.read_text(), '# initial gateway\n')

    def test_gateway_repair_rejects_changed_previous_manifest(self):
        self.prepare_gateway_repair()
        previous = self.package / 'previous-payload-manifest.json'
        previous.write_text(previous.read_text() + '\n')
        before = self.installer.path(INSTALL.PREFIX + '/' + INSTALL.GATEWAY_MODULE).read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'Previous payload manifest'):
            self.installer.repair_gateway()
        self.assertEqual(self.installer.path(INSTALL.PREFIX + '/' + INSTALL.GATEWAY_MODULE).read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_gateway_repair_rejects_changed_recorded_old_manifest(self):
        self.prepare_gateway_repair()
        state = self.installer.load()
        state['payload_manifest']['files']['run.py'] = 'f' * 64
        self.installer.save(state)
        with self.assertRaisesRegex(RuntimeError, 'Previous payload manifest'):
            self.installer.repair_gateway()
        self.assertEqual(self.calls, [])

    def test_gateway_repair_rejects_modified_installed_module(self):
        self.prepare_gateway_repair()
        self.installer.path(INSTALL.PREFIX + '/' + INSTALL.GATEWAY_MODULE).write_text('# local edit\n')
        with self.assertRaisesRegex(RuntimeError, 'Installed candidate changed'):
            self.installer.repair_gateway()
        self.assertEqual(self.calls, [])

    def test_gateway_repair_rejects_other_payload_and_symlink_changes(self):
        self.prepare_gateway_repair()
        path = self.package / 'payload/run.py'
        original = path.read_bytes()
        path.write_text('# unrelated new file\n')
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeError, 'only takeover/login_gateway.py'):
            self.installer.repair_gateway()
        path.write_bytes(original)
        alias = self.package / 'payload/alias.py'
        alias.unlink()
        alias.symlink_to('takeover/login_gateway.py')
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeError, 'only takeover/login_gateway.py'):
            self.installer.repair_gateway()
        self.assertEqual(self.calls, [])

    def test_nonidle_repair_does_not_replace_module_or_state(self):
        self.prepare_gateway_repair()
        source = self.installer.path(INSTALL.PREFIX + '/' + INSTALL.GATEWAY_MODULE)
        original, record = source.read_bytes(), self.installer.state_file.read_bytes()
        with patch.object(self.installer, 'idle_snapshot', side_effect=RuntimeError('remote lease')):
            with self.assertRaisesRegex(RuntimeError, 'remote lease'):
                self.installer.repair_gateway()
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(self.installer.state_file.read_bytes(), record)
        self.assertFalse(any('restart' in call for call in self.calls))

    def test_gateway_repair_resumes_after_source_replace_before_state_commit(self):
        self.prepare_gateway_repair()
        real_save = self.installer.save
        def interrupted(state):
            if state['phase'] == 'installed' and state.get('gateway_repair', {}).get('status') == 'complete':
                raise RuntimeError('simulated interruption before state commit')
            real_save(state)
        with patch.object(self.installer, 'idle_snapshot', return_value={'gnome_pid': 10}), \
             patch.object(self.installer, 'save', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
                self.installer.repair_gateway()
        self.assertEqual(self.installer.load()['phase'], 'gateway-repairing')
        with patch.object(self.installer, 'idle_snapshot', return_value={'gnome_pid': 10}), \
             patch.object(self.installer, 'wait_gateway', return_value={'version': 1, 'endpoints': ['desktop']}):
            self.installer.repair_gateway()
        self.assertEqual(self.installer.load()['phase'], 'active')
        self.installer.assert_current(self.installer.load())

    def slow_gateway_properties(self, now, finish):
        pending = bool(self.gateway_invocation) and now[0] < finish
        return {'MainPID': '40' if pending or not self.gateway_invocation else '41',
                'InvocationID': 'old' if pending or not self.gateway_invocation else 'new',
                'Job': '77' if pending else '0',
                'ActiveState': 'deactivating' if pending else 'active',
                'SubState': 'stop-sigterm' if pending else 'running', 'TimeoutStopUSec': '10s'}

    def test_slow_old_stop_waits_for_systemd_job_without_premature_fallback(self):
        self.install()
        self.calls.clear()
        now = [0.]
        with patch.object(self.installer, 'unit_properties', side_effect=lambda *args, **kwargs: self.slow_gateway_properties(now, 95)), \
             patch.object(self.installer, 'idle_snapshot', return_value={'gnome_pid': 10}), \
             patch.object(self.installer, 'wait_gateway', return_value={'version': 1, 'endpoints': ['desktop']}), \
             patch.object(INSTALL.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(INSTALL.time, 'sleep', side_effect=lambda value: now.__setitem__(0, now[0] + value)):
            self.installer.activate_gateway()
        self.assertGreaterEqual(now[0], 95)
        self.assertEqual([call for call in self.calls if 'restart' in call],
                         [['systemctl', '--no-block', 'restart', 'ubuntu-native-login.service']])
        self.assertEqual(self.installer.load()['phase'], 'active')

    def test_pending_job_timeout_never_restores_or_submits_competing_fallback(self):
        self.install()
        self.calls.clear()
        now = [0.]
        with patch.object(self.installer, 'unit_properties', side_effect=lambda *args, **kwargs: self.slow_gateway_properties(now, 1000)), \
             patch.object(self.installer, 'idle_snapshot', return_value={'gnome_pid': 10}), \
             patch.object(INSTALL.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(INSTALL.time, 'sleep', side_effect=lambda value: now.__setitem__(0, now[0] + value)):
            with self.assertRaisesRegex(RuntimeError, 'no competing fallback restart'):
                self.installer.activate_gateway()
        self.assertEqual(now[0], 130)
        self.assertEqual(self.installer.load()['phase'], 'gateway-pending')
        self.assertIn(INSTALL.PREFIX, self.installer.path(INSTALL.GATEWAY).read_text())
        self.assertEqual([call for call in self.calls if 'restart' in call],
                         [['systemctl', '--no-block', 'restart', 'ubuntu-native-login.service']])

    def test_restart_refuses_to_submit_over_existing_job(self):
        with patch.object(self.installer, 'unit_properties', return_value={
                'Job': '17', 'ActiveState': 'deactivating', 'SubState': 'stop-sigterm'}):
            with self.assertRaises(INSTALL.GatewayJobPending):
                self.installer.restart_gateway()
        self.assertEqual(self.calls, [])

    def test_restart_client_timeout_observes_accepted_job_without_resubmission(self):
        now = [0.]
        def submitted(arguments, **kwargs):
            self.calls.append(arguments)
            self.gateway_invocation += 1
            raise subprocess.TimeoutExpired(arguments, 30)
        self.installer.runner = submitted
        with patch.object(self.installer, 'unit_properties', side_effect=lambda *args, **kwargs: self.slow_gateway_properties(now, 95)), \
             patch.object(INSTALL.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(INSTALL.time, 'sleep', side_effect=lambda value: now.__setitem__(0, now[0] + value)):
            result = self.installer.restart_gateway()
        self.assertEqual(result['InvocationID'], 'new')
        self.assertEqual(len(self.calls), 1)

    def test_activation_requires_loaded_stop_timeout_before_restart(self):
        self.install()
        self.calls.clear()
        with patch.object(self.installer, 'unit_properties', return_value={
                'Job': '0', 'ActiveState': 'active', 'MainPID': '40', 'TimeoutStopUSec': '1min 30s'}), \
             patch.object(self.installer, 'idle_snapshot', return_value={'gnome_pid': 10}):
            with self.assertRaisesRegex(RuntimeError, 'TimeoutStopSec=10s was not loaded'):
                self.installer.activate_gateway()
        self.assertFalse(any('restart' in call for call in self.calls))


if __name__ == '__main__':
    unittest.main()
