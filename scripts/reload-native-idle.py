"""One-shot deployment: reload RDP only after the current client disconnects."""
import json
from pathlib import Path
import socket
import subprocess
import time
import os

gate = f'/run/user/{os.getuid()}/ubuntu-native-takeover/gate.sock'
idle_since = None
while True:
    try:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(3)
            sock.connect(gate)
            sock.sendall(b'STATUS\n')
            state = json.loads(sock.recv(1024))
        connections = subprocess.check_output(
            ['ss', '-Htn', 'state', 'established', '( sport = :3392 )'], text=True).strip()
        idle = state['native_state'] == 'local' and not state['remote'] and not connections
    except (OSError, ValueError):
        idle = False
    if idle:
        if idle_since is None:
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since >= 2:
            subprocess.run(['systemctl', '--user', 'restart', 'ubuntu-native-takeover.service'], check=True)
            result = Path(__file__).resolve().parents[1] / 'artifacts/adaptive-reload-result.json'
            result.write_text(json.dumps({'rdp_reloaded_when_idle': True, 'timestamp': time.time()}) + '\n')
            print('Adaptive RDP update loaded after client disconnect.', flush=True)
            break
    else:
        idle_since = None
    time.sleep(1)
