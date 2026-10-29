"""Real authenticated RDP traffic through patched GRD and the production broker."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time


def run(root, runtime, env, viewer_env, connector, app_pid, app_state, spawn, stop, wait_for):
    artifacts = root / 'artifacts'
    gate = runtime / 'gate.sock'
    gate.unlink(missing_ok=True)
    physical = runtime / 'physical.json'
    status = runtime / 'gate-status.json'
    physical.write_text(json.dumps({'locked': False, 'locks': 0}))
    status.unlink(missing_ok=True)
    broker = spawn(['/usr/bin/python3', str(root / 'tests/broker_host.py'), str(gate),
                    str(physical), str(status), str(root / 'build/session-viewer'),
                    '--attach-monitor', connector, '--target-bus', env['DBUS_SESSION_BUS_ADDRESS']],
                   viewer_env, 'gate-broker')
    wait_for(gate.exists, 'gate did not start')

    def request(verb):
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(10)
            s.connect(str(gate))
            s.sendall(verb.encode() + b'\n')
            return s.recv(4096)

    def state():
        return json.loads(request('STATUS'))

    assert request('LOCAL') == b'OK\n'
    wait_for(lambda: state()['viewer_pid'] is not None, 'local viewer missing')
    viewer_pid = state()['viewer_pid']
    time.sleep(2)
    password = secrets.token_urlsafe(24)
    grd_env = dict(env, GSETTINGS_BACKEND='keyfile', GRD_TAKEOVER_SOCKET=str(gate),
                   GNOME_REMOTE_DESKTOP_TEST_RDP_USERNAME='takeover-test',
                   GNOME_REMOTE_DESKTOP_TEST_RDP_PASSWORD=password,
                   LD_LIBRARY_PATH=str(root / 'vendor/grd-deps/root/usr/lib/x86_64-linux-gnu'))
    subprocess.run(['gsettings', 'set', 'org.gnome.desktop.remote-desktop.rdp.headless', 'enable', 'true'],
                   env=grd_env, check=True)
    grd = spawn([str(root / 'build/grd-prefix/libexec/gnome-remote-desktop-daemon'),
                 '--headless', '--rdp-port=43390'], grd_env, 'gate-grd')
    wait_for(lambda: 'RDP server started' in (artifacts / 'gate-grd.log').read_text(), 'patched GRD failed')
    xfreerdp = root / 'vendor/ubuntu-freerdp/root/usr/bin/xfreerdp'

    def remote(name, pwd):
        return spawn([str(xfreerdp), '/v:127.0.0.1:43390', '/u:takeover-test', '/p:' + pwd,
                      '/cert:ignore', '/size:1280x720', '/gfx:RFX', '/network:lan',
                      '/title:' + name], viewer_env, name)

    bad = remote('gate-wrong-password', 'wrong-' + secrets.token_urlsafe(8))
    wait_for(lambda: bad.poll() is not None, 'incorrect password did not fail', 15)
    assert json.loads(physical.read_text())['locks'] == 0, 'unauthenticated client locked local desktop'
    assert state()['viewer_pid'] == viewer_pid and not state()['remote']
    rdp = remote('GateRDPTest', password)
    wait_for(lambda: state()['remote'], 'authenticated remote did not acquire lease', 20)
    assert state()['viewer_pid'] is None and json.loads(physical.read_text())['locked']
    assert not Path(f'/proc/{viewer_pid}').exists(), 'old local viewer still alive at grant'
    time.sleep(2)

    def xd(*args):
        return subprocess.check_output(['xdotool', *args], env=viewer_env, text=True).strip()

    wid = xd('search', '--onlyvisible', '--name', '^GateRDPTest$').splitlines()[-1]
    xd('windowfocus', wid)
    xd('mousemove', '--window', wid, '450', '295')
    xd('click', '1')
    time.sleep(.5)
    xd('key', 'ctrl+a')
    xd('type', '--clearmodifiers', '--delay', '80', 'exclusive-session-789')
    wait_for(lambda: json.loads(app_state.read_text())['text'] == 'exclusive-session-789', 'exclusive RDP input failed')
    assert json.loads(app_state.read_text())['pid'] == app_pid
    time.sleep(11)  # A completed handshake must not time out an idle lease.
    assert rdp.poll() is None and state()['remote'], 'healthy idle lease timed out'
    assert request('LOCAL') == b'DENIED!\n', 'locked local desktop acquired viewer'
    competitor = remote('GateCompetingClient', password)
    wait_for(lambda: competitor.poll() is not None, 'second remote was not refused', 15)
    assert state()['remote']

    p = json.loads(physical.read_text())
    p['locked'] = False  # Only physical PAM authentication would do this in production.
    physical.write_text(json.dumps(p))
    wait_for(lambda: state()['viewer_pid'] is not None and not state()['remote'], 'local unlock did not revoke RDP', 15)
    wait_for(lambda: rdp.poll() is not None, 'revoked client remained connected')
    assert json.loads(app_state.read_text())['text'] == 'exclusive-session-789'
    rdp2 = remote('GateRDPReconnect', password)
    wait_for(lambda: state()['remote'], 'second takeover failed', 20)
    broker.kill()  # Real broker crash: GRD must stop on lease EOF.
    broker.wait()
    wait_for(lambda: rdp2.poll() is not None, 'broker crash left RDP connected', 15)
    assert json.loads(physical.read_text())['locked']
    assert json.loads(app_state.read_text())['pid'] == app_pid
    stop(grd)
    result = {'wrong_password_does_not_lock_local': True,
              'authenticated_remote_revokes_local_before_grant': True,
              'exclusive_rdp_input_verified': True, 'second_remote_rejected': True,
              'locked_local_attach_rejected': True,
              'local_unlock_revokes_remote_before_viewer': True,
              'broker_crash_disconnects_remote': True, 'application_pid_preserved': app_pid,
              'idle_lease_survives_handshake_timeout': True,
              'physical_lock_backend': 'simulated; see live-result.json for physical GNOME test'}
    (artifacts / 'takeover-gate-result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2), flush=True)
    return result
