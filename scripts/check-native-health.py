#!/usr/bin/python3
"""Read-only checks of the native 3392 entry; TCP probing is opt-in and idle-only."""
import argparse
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import struct
import subprocess
import sys


PORTS = (3392, 3393)


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f'{args[0]} returned {result.returncode}')
    return result.stdout


def properties(text):
    return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)


def unit_status(name, user=False):
    args = ['systemctl'] + (['--user'] if user else [])
    return properties(command(args + ['show', name, '--no-pager',
        '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState', '-p', 'Result',
        '-p', 'MainPID', '-p', 'NRestarts', '-p', 'WatchdogUSec']))


def parse_listeners(text, ipv6=False):
    """Decode Linux procfs socket addresses without opening a TCP connection."""
    found = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[3] != '0A':
            continue
        address, port = fields[1].split(':')
        port = int(port, 16)
        if port not in PORTS:
            continue
        packed = bytes.fromhex(address)
        if ipv6:
            packed = struct.pack('!4I', *struct.unpack('=4I', packed))
        else:
            packed = struct.pack('!I', struct.unpack('=I', packed)[0])
        address = socket.inet_ntop(socket.AF_INET6 if ipv6 else socket.AF_INET, packed)
        ip = ipaddress.ip_address(address)
        loopback = ip.is_loopback or bool(getattr(ip, 'ipv4_mapped', None)
                                         and ip.ipv4_mapped.is_loopback)
        found.append({'address': address, 'port': port, 'loopback': loopback})
    return found


def listeners():
    found = []
    for path, ipv6 in (('/proc/net/tcp', False), ('/proc/net/tcp6', True)):
        try:
            found.extend(parse_listeners(Path(path).read_text(), ipv6))
        except FileNotFoundError:
            if not ipv6:
                raise
    return found


def broker_status():
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(3)
        sock.connect(f'/run/user/{os.getuid()}/ubuntu-native-takeover/gate.sock')
        sock.sendall(b'STATUS\n')
        data = bytearray()
        while b'\n' not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError('Broker closed before sending a complete STATUS response')
            data.extend(chunk)
            if len(data) > 4096:
                raise RuntimeError('Broker STATUS response exceeds 4096 bytes')
        result = json.loads(bytes(data).split(b'\n', 1)[0])
        if not isinstance(result, dict) or not {'native_state', 'remote', 'session'} <= result.keys():
            raise RuntimeError('Invalid broker STATUS response')
        return result


def physical_sessions():
    sessions = json.loads(command(['loginctl', 'list-sessions', '--json=short']))
    result = []
    for session in sessions:
        if session.get('seat') != 'seat0':
            continue
        detail = properties(command(['loginctl', 'show-session', str(session['session']),
            '-p', 'Id', '-p', 'User', '-p', 'Class', '-p', 'Type', '-p', 'Active',
            '-p', 'Remote', '-p', 'LockedHint']))
        result.append(detail)
    return result


def assert_probe_idle():
    status = broker_status()
    if status.get('native_state') != 'local' or status.get('remote') is not False:
        raise RuntimeError('TLS probe skipped: the desktop is not locally idle')
    connections = command(['ss', '-Htn', 'state', 'established',
                           '( sport = :3392 or sport = :3393 )']).strip()
    if connections:
        raise RuntimeError('TLS probe skipped: a 3392/3393 connection already exists')


def receive_exact(sock, count):
    result = bytearray()
    while len(result) < count:
        data = sock.recv(count - len(result))
        if not data:
            raise RuntimeError('Unexpected EOF during RDP negotiation')
        result.extend(data)
    return bytes(result)


def probe_tls(port):
    assert_probe_idle()
    with socket.create_connection(('127.0.0.1', port), timeout=3) as sock:
        # SSL | HYBRID request; no username, password, or desktop lease.
        sock.sendall(bytes.fromhex('030000130ee000000000000100080003000000'))
        header = receive_exact(sock, 4)
        size = int.from_bytes(header[2:], 'big')
        if header[:2] != b'\x03\x00' or not 12 <= size <= 4096:
            raise RuntimeError('Invalid RDP TPKT response')
        response = receive_exact(sock, size - 4)
        if response[-8] != 2 or response[-6:-4] != b'\x08\x00':
            raise RuntimeError('RDP protocol negotiation was refused')
        protocol = int.from_bytes(response[-4:], 'little')
        if protocol not in (1, 2):
            raise RuntimeError(f'Unexpected selected RDP protocol: {protocol}')
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with context.wrap_socket(sock, server_hostname='localhost') as tls:
            return {'port': port, 'x224': 'passed', 'selected_protocol': protocol,
                    'tls_version': tls.version(),
                    'certificate_sha256': hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest(),
                    'certificate_verified': False, 'authentication_tested': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tls', action='store_true',
        help='When idle, also test loopback X224/TLS; never authenticate or unlock')
    args = parser.parse_args()
    report = {'time': datetime.datetime.now().astimezone().isoformat(), 'errors': {},
              'boundary': ('Local service/readiness checks only; default checks do not exercise gateway '
                           'routing. No external-network, remote-client, or account-login validation.'),
              'tcp_probe_requested': args.tls}
    checks = {'gateway': lambda: unit_status('ubuntu-native-login.service'),
              'backend': lambda: unit_status('ubuntu-native-takeover.service', user=True),
              'listeners': listeners, 'broker': broker_status, 'physical_sessions': physical_sessions}
    for name, check in checks.items():
        try:
            report[name] = check()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            report['errors'][name] = str(error)
    bound = report.get('listeners', [])
    backend_bound = [item for item in bound if item['port'] == 3393]
    report['checks'] = {
        'gateway_active': report.get('gateway', {}).get('ActiveState') == 'active',
        'backend_active': report.get('backend', {}).get('ActiveState') == 'active',
        'gateway_listening': any(item['port'] == 3392 for item in bound),
        'backend_listening_only_on_loopback': bool(backend_bound)
            and all(item['loopback'] for item in backend_bound),
        'broker_responding': 'broker' in report,
        'active_physical_wayland_session': any(
            s.get('User') == str(os.getuid()) and s.get('Class') == 'user'
            and s.get('Type') == 'wayland' and s.get('Active') == 'yes'
            and s.get('Remote') == 'no' for s in report.get('physical_sessions', [])),
    }
    if args.tls:
        report['tls_boundary'] = ('Availability only; server certificate verification is disabled. '
                                  'Closing before authentication may generate expected RDP transport errors.')
        report['tls'] = []
        for port in PORTS:
            try:
                report['tls'].append(probe_tls(port))
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                report['errors'][f'tls_{port}'] = str(error)
                break
    report['server_checks_passed'] = all(report['checks'].values()) and not report['errors']
    print(json.dumps(report, ensure_ascii=False, separators=(',', ':')))
    return 0 if report['server_checks_passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
