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


def client_scale(percent):
    """Scale announced by the RDP client, as a supported remote scale.

    Clients send 100..500 percent; 0 or anything else means "not announced".
    """
    if isinstance(percent, bool) or not isinstance(percent, int) or not 100 <= percent <= 500:
        return None
    return min(SCALES, key=lambda scale: (abs(scale - percent), scale))


def _load():
    try:
        settings = json.loads(settings_path().read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(settings, dict):
        raise ValueError('Remote settings must be a JSON object')
    return settings


def load_scale():
    settings = _load()
    return validate_scale(settings['scale_percent']) if 'scale_percent' in settings else DEFAULT_SCALE


def load_multi_monitor():
    """Extra client monitors as remote-only virtual monitors (default on)."""
    value = _load().get('multi_monitor', True)
    if not isinstance(value, bool):
        raise ValueError('multi_monitor must be true or false')
    return value


def save_scale(percent):
    _save(scale_percent=validate_scale(percent))


def save_multi_monitor(enabled):
    if not isinstance(enabled, bool):
        raise ValueError('multi_monitor must be true or false')
    _save(multi_monitor=enabled)


def _save(**values):
    try:
        settings = _load()
    except ValueError:
        settings = {}
    settings.update(values)
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    import tempfile
    fd, name = tempfile.mkstemp(prefix='.remote-settings-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(settings, f)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
