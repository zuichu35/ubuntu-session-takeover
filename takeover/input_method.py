"""Separate writable input-method state for the persistent desktop."""
import shutil
from pathlib import Path
from takeover.common import DATA, workspace_env


def prepare():
    base = DATA / 'input-method'
    config = base / 'config/fcitx5'
    data = base / 'data/fcitx5'
    for source, dest in [(Path.home() / '.config/fcitx5', config),
                         (Path.home() / '.local/share/fcitx5', data)]:
        if not dest.exists():
            if source.exists():
                # Live LevelDB databases cannot be copied consistently while
                # their original input method is running. Start new user DBs.
                shutil.copytree(source, dest, ignore=shutil.ignore_patterns('*.userdb', 'sync', '.git'))
            else:
                dest.mkdir(parents=True, mode=0o700)
    marker = base / 'prepared'
    if not marker.exists():
        (config / 'profile').write_text(
            '[Groups/0]\nName=Default\nDefault Layout=us\nDefaultIM=rime\n\n'
            '[Groups/0/Items/0]\nName=keyboard-us\nLayout=\n\n'
            '[Groups/0/Items/1]\nName=rime\nLayout=\n\n[GroupOrder]\n0=Default\n')
        marker.touch()
    return dict(workspace_env(), XDG_CONFIG_HOME=str(base / 'config'), XDG_DATA_HOME=str(base / 'data'))
