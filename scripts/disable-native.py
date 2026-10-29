#!/usr/bin/env python3
"""Undo next-login native integration without logging out or killing desktop apps."""
import json
import hashlib
from pathlib import Path
import subprocess

config = Path.home() / '.config/ubuntu-native-takeover'
manifest = config / 'install.json'
saved = json.loads(manifest.read_text())
for name, expected in saved.get('installed_files', {}).items():
    path = Path(name)
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise RuntimeError('Deployment file changed after installation; preserve and review it: ' + name)
legacy = Path.home() / '.config/autostart/ubuntu-takeover.desktop'
if saved.get('legacy_autostart') is not None and legacy.exists():
    expected = saved['legacy_autostart'].rstrip() + '\nHidden=true\n'
    if legacy.read_text() != expected:
        raise RuntimeError('Legacy autostart changed after installation; refusing to overwrite it')
subprocess.run(['systemctl', '--user', 'stop', 'ubuntu-native-takeover.service'], check=True)
for name, previous in saved['files'].items():
    path = Path(name)
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(previous)
if saved.get('legacy_autostart') is not None:
    legacy.write_text(saved['legacy_autostart'])
if saved.get('legacy_enabled_units'):
    subprocess.run(['systemctl', '--user', 'enable', *saved['legacy_enabled_units']], check=True)
subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
manifest.rename(config / 'install.disabled.json')
print('Native startup removed. Next login uses system Mutter; current desktop apps were preserved.')
