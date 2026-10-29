"""Remote-only preferences; never modify the local display configuration."""
import json
import os
from pathlib import Path

SCALES = (100, 125, 150, 175, 200)
DEFAULT_SCALE = 150


def settings_path():
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'ubuntu-native-takeover/remote-settings.json'


def validate_scale(percent):
    if isinstance(percent, bool) or percent not in SCALES:
        raise ValueError('Scale must be 100, 125, 150, 175 or 200 percent')
    return int(percent)


def load_scale():
    try:
        return validate_scale(json.loads(settings_path().read_text())['scale_percent'])
    except FileNotFoundError:
        return DEFAULT_SCALE


def save_scale(percent):
    percent = validate_scale(percent)
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    import tempfile
    fd, name = tempfile.mkstemp(prefix='.remote-settings-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump({'scale_percent': percent}, f)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
