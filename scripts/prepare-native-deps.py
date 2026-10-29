#!/usr/bin/env python3
"""Extract missing build dependencies locally without installing/upgrading the OS."""
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1] / 'vendor/native-deps'
packages = root / 'packages'
packages.mkdir(parents=True, exist_ok=True)
requested = '''gsettings-desktop-schemas-dev libxkbregistry-dev libdisplay-info-dev
libcolord-dev libgnome-desktop-4-dev libinput-dev libwacom-dev libxcb-res0-dev
libcanberra-dev libstartup-notification0-dev libudev-dev libgudev-1.0-dev
libxkbfile-dev libevdev-dev libmtdev-dev libatk1.0-dev libatspi2.0-dev libeis-dev
libei-dev libxcomposite-dev libxcursor-dev libxdamage-dev libxfixes-dev libxi-dev
libxrandr-dev libxinerama-dev libx11-xcb-dev libgbm-dev systemd-dev gettext python3-docutils libpam0g-dev'''.split()
simulation = subprocess.check_output(['apt-get', '-s', 'install', *requested], text=True)
missing = [line.split()[1] for line in simulation.splitlines() if line.startswith('Inst ')]
if missing:
    subprocess.run(['apt-get', 'download', *missing], cwd=packages, check=True)
for deb in packages.glob('*.deb'):
    subprocess.run(['dpkg-deb', '-x', str(deb), str(root / 'root')], check=True)
prefix = root / 'root/usr'
for pc in prefix.rglob('*.pc'):
    pc.write_text(pc.read_text().replace('prefix=/usr', 'prefix=' + str(prefix))
                  .replace('-I/usr/include', '-I' + str(prefix / 'include'))
                  .replace('-L/usr/lib', '-L' + str(prefix / 'lib')))
for link in prefix.rglob('*.so'):
    if link.is_symlink() and not link.exists():
        target = Path('/usr') / link.relative_to(prefix).parent / link.readlink().name
        if target.exists():
            link.unlink()
            link.symlink_to(target)
print('Native dependency extraction complete; no system packages changed.')
