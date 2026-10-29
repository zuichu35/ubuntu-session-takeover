"""Read-only GRD assets shared by GDM's static and dynamic greeter accounts.

Only the GRD child uses these paths. The greeter's HOME, session bus, writable
runtime directory and normal dconf profile must remain private to its session.
"""
import grp
import os
from pathlib import Path
import stat


CONFIG = Path('/etc/ubuntu-native-login/greeter')
DATA = Path('/var/lib/ubuntu-native-greeter')


def dconf_profile(config=CONFIG):
    return f'file-db:{Path(config) / "settings.db"}\n'


def dconf_settings(data=DATA):
    data = Path(data)
    return f'''[org/gnome/desktop/remote-desktop/rdp]
tls-cert='{data / "tls.crt"}'
tls-key='{data / "tls.key"}'
view-only=false

[org/gnome/desktop/remote-desktop/rdp/headless]
enable=true
port=uint16 3394
negotiate-port=false
auth-methods=['credentials']

[org/gnome/desktop/remote-desktop/vnc/headless]
enable=false
'''


DCONF_LOCKS = '/org/gnome/desktop/remote-desktop/\n'


def _check(path, *, directory=False, secret=False, owner_uid=0, group_gid=None):
    """Reject missing or mutable assets without inspecting credential contents."""
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise RuntimeError(f'Missing native greeter asset: {path}; run the root greeter installer') from error
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not kind_ok:
        raise RuntimeError(f'Native greeter asset is not a regular {"directory" if directory else "file"}: {path}')
    if info.st_uid != owner_uid or info.st_mode & 0o022:
        raise RuntimeError(f'Native greeter asset must be root-owned and not group/world-writable: {path}')
    if secret and (info.st_mode & 0o007 or not info.st_mode & 0o040 or info.st_gid != group_gid):
        raise RuntimeError(f'Native greeter secret must be restricted to root and the gdm group: {path}')
    if not directory and info.st_size == 0:
        raise RuntimeError(f'Native greeter asset is empty: {path}')
    if not os.access(path, os.R_OK | (os.X_OK if directory else 0)):
        raise RuntimeError(f'Native greeter asset is not readable by this greeter account: {path}')


def grd_environment(environment, *, config=CONFIG, data=DATA):
    """Validate installed assets and return a child-only environment copy."""
    config, data = Path(config), Path(data)
    try:
        gdm_gid = grp.getgrnam('gdm').gr_gid
    except KeyError as error:
        raise RuntimeError('Missing gdm group; native greeter assets cannot be authenticated') from error
    directories = {config, data, data / 'rdp', data / 'rdp/gnome-remote-desktop',
                   *config.parents, *data.parents}
    for directory in sorted(directories, key=lambda path: (len(path.parts), str(path))):
        _check(directory, directory=True)
    for path in (config / 'dconf-profile', config / 'settings.db', data / 'tls.crt'):
        _check(path)
    for path in (data / 'tls.key', data / 'rdp/gnome-remote-desktop/credentials.ini'):
        _check(path, secret=True, group_gid=gdm_gid)
    if (config / 'dconf-profile').read_text() != dconf_profile(config):
        raise RuntimeError('Native greeter dconf profile must reference only its installed read-only settings.db')
    result = dict(environment)
    result.update(DCONF_PROFILE=str(config / 'dconf-profile'), XDG_DATA_HOME=str(data / 'rdp'))
    return result
