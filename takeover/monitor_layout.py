"""Temporary mirrored layout for native RDP, with crash recovery.

Only method=1 is used: monitors.xml and the user's persistent layout are not
rewritten. A runtime journal is written before touching the compositor.
"""
import json
import math
import os
from pathlib import Path
import time

from gi.repository import Gio, GLib
from takeover.remote_settings import SCALES, load_scale, validate_scale

NAME = 'org.gnome.Mutter.DisplayConfig'
OBJECT = '/org/gnome/Mutter/DisplayConfig'


def virtual_connectors(state):
    """Remote-only monitors: never saved, restored or used as viewport passengers."""
    return {spec[0] for spec, _, p in state[1] if p.get('native-takeover-remote-only')}


def snapshot(state):
    _, monitors, logical, props = state
    by_connector = {spec[0]: (spec, modes, p) for spec, modes, p in monitors}
    virtual = virtual_connectors(state)
    result = {'logical': [], 'properties': {}, 'identities': {}}
    if props.get('supports-changing-layout-mode'):
        result['properties']['layout-mode'] = props['layout-mode']
    if 'native-takeover-size' in props:
        result['properties']['native-takeover-size'] = list(props['native-takeover-size'])
    if 'native-takeover-extra' in props:
        result['properties']['native-takeover-extra'] = [list(e) for e in props['native-takeover-extra']]
    for x, y, scale, transform, primary, specs, _ in logical:
        specs = [spec for spec in specs if spec[0] not in virtual]
        if not specs:
            continue
        outputs = []
        for spec in specs:
            _, modes, p = by_connector[spec[0]]
            mode = next(m[0] for m in modes if m[6].get('is-current'))
            values = {}
            if 'is-underscanning' in p:
                values['underscanning'] = p['is-underscanning']
            if 'color-mode' in p:
                values['color-mode'] = p['color-mode']
            outputs.append([spec[0], mode, values])
            result['identities'][spec[0]] = list(spec)
        result['logical'].append([x, y, scale, transform, primary, outputs])
    return result


def single_monitor(saved):
    primary = next((m for m in saved['logical'] if m[4]), None)
    if primary is None:
        raise RuntimeError('No primary monitor; refusing to change layout')
    # One physical connector even if the primary logical monitor is a mirror.
    return dict(saved, logical=[[0, 0, primary[2], primary[3], True, [primary[5][0]]]])


def adaptive_monitor(state, width, height):
    if not 200 <= width <= 8192 or not 200 <= height <= 8192:
        raise ValueError('Resolution outside 200..8192')
    target = single_monitor(snapshot(state))
    output = target['logical'][0][5][0]
    modes = next(modes for spec, modes, _ in state[1] if spec[0] == output[0])
    candidates = []
    for mode in modes:
        if mode[6].get('refresh-rate-mode', 'fixed') != 'fixed':
            continue
        for rotation in (0, 1):
            w, h = (mode[1], mode[2]) if rotation == 0 else (mode[2], mode[1])
            score = abs(math.log(w / width)) + abs(math.log(h / height))
            score += abs(math.log((w / h) / (width / height)))
            candidates.append(((score, abs(mode[3] - 60), rotation), mode, rotation))
    if not candidates:
        raise RuntimeError('No fixed refresh mode available')
    _, mode, rotation = min(candidates, key=lambda item: item[0])
    target['logical'][0][2] = min(mode[5], key=lambda scale: abs(scale - 1))
    target['logical'][0][3] = rotation
    output[1] = mode[0]
    return target, ((mode[1], mode[2]) if rotation == 0 else (mode[2], mode[1]))


def mirrored_monitor(state, width, height):
    """One captured logical monitor while keeping every active output powered."""
    if not 200 <= width <= 8192 or not 200 <= height <= 8192:
        raise ValueError('Resolution outside 200..8192')
    saved = snapshot(state)
    primary = next(m for m in saved['logical'] if m[4])
    outputs = [primary[5][0]] + [o for m in saved['logical'] for o in m[5]
                                   if o[0] != primary[5][0][0]]
    if len(outputs) == 1:
        return adaptive_monitor(state, width, height)
    mode_maps = []
    for output in outputs:
        modes = next(modes for spec, modes, _ in state[1] if spec[0] == output[0])
        sizes = {}
        for mode in modes:
            if mode[6].get('refresh-rate-mode', 'fixed') != 'fixed':
                continue
            key = (mode[1], mode[2])
            if key not in sizes or abs(mode[3] - 60) < abs(sizes[key][3] - 60):
                sizes[key] = mode
        mode_maps.append(sizes)
    common = set.intersection(*(set(modes) for modes in mode_maps))
    candidates = []
    for size in common:
        modes = [mode_map[size] for mode_map in mode_maps]
        scales = set.intersection(*(set(mode[5]) for mode in modes))
        if not scales:
            continue
        for rotation in (0, 1):
            w, h = size if rotation == 0 else size[::-1]
            score = abs(math.log(w / width)) + abs(math.log(h / height))
            score += abs(math.log((w / h) / (width / height)))
            candidates.append(((score, sum(abs(m[3] - 60) for m in modes), rotation),
                               modes, min(scales, key=lambda s: abs(s - 1)), rotation, (w, h)))
    if not candidates:
        raise RuntimeError('Active displays have no common mirror mode; refusing to turn off a local output')
    _, modes, scale, rotation, actual = min(candidates, key=lambda c: c[0])
    mirrored = [[output[0], mode[0], output[2]] for output, mode in zip(outputs, modes)]
    return dict(saved, logical=[[0, 0, scale, rotation, True, mirrored]]), actual


def remote_monitor(state, width, height, scale_percent=100, extras=()):
    """Exact offscreen desktop with real physical modes kept for local reclaim.

    The compositor advertises this only with the native privacy implementation.
    Older running compositors continue using the supported-mode fallback.
    """
    if not state[3].get('native-takeover-viewport-supported'):
        return mirrored_monitor(state, width, height)
    if not 200 <= width <= 8192 or not 200 <= height <= 8192:
        raise ValueError('Resolution outside 200..8192')
    validate_scale(scale_percent)
    if scale_percent != 100 and not state[3].get('native-takeover-scale-supported'):
        raise RuntimeError('Remote scaling requires the new Mutter; log out and log in once')
    saved = snapshot(state)
    primary = next(m for m in saved['logical'] if m[4])
    outputs = [primary[5][0]] + [o for m in saved['logical'] for o in m[5]
                                   if o[0] != primary[5][0][0]]
    saved['properties']['native-takeover-size'] = [width, height]
    # Extra client monitors are Mutter virtual monitors placed beside the
    # viewport. Older compositors keep the single viewport.
    saved['properties'].pop('native-takeover-extra', None)
    if state[3].get('native-takeover-multimonitor-supported'):
        saved['properties']['native-takeover-extra'] = [list(e) for e in extras]
    return dict(saved, logical=[[0, 0, scale_percent / 100., 0, True, outputs]]), (width, height)


def arguments(serial, config, method=1):
    logical = []
    for x, y, scale, transform, primary, monitors in config['logical']:
        outputs = [(connector, mode, {k: GLib.Variant('b' if k == 'underscanning' else 'u', v)
                                     for k, v in props.items()})
                   for connector, mode, props in monitors]
        logical.append((x, y, scale, transform, primary, outputs))
    types = {'native-takeover-size': '(uu)', 'native-takeover-extra': 'a(iiuu)',
             'native-takeover-release': 'b'}
    props = {k: GLib.Variant(types.get(k, 'u'), tuple(map(tuple, v)) if k == 'native-takeover-extra' else v)
             for k, v in config['properties'].items()}
    return GLib.Variant('(uua(iiduba(ssa{sv}))a{sv})', (serial, method, logical, props))


class MonitorLayout:
    def __init__(self, connection, journal, scale_percent=None):
        self.connection = connection
        self.journal = Path(journal)
        # None follows the user's saved setting; the greeter has a fixed one.
        self.fallback_scale = None if scale_percent is None else validate_scale(scale_percent)
        self.scale_percent = load_scale() if scale_percent is None else self.fallback_scale
        self.extras = ()

    def begin_scale(self, client_percent=None):
        """Scale of a new lease: the client's, else the saved fallback."""
        if client_percent is not None:
            self.scale_percent = validate_scale(client_percent)
        elif self.fallback_scale is not None:
            self.scale_percent = self.fallback_scale
        else:
            try:
                self.scale_percent = load_scale()
            except ValueError:
                pass  # A damaged settings file keeps the previous scale.

    @staticmethod
    def viewport_scale(state):
        """Scale of the live viewport; GNOME Settings may have changed it."""
        if 'native-takeover-size' not in state[3]:
            return None
        primary = next((m for m in state[2] if m[4]), None)
        percent = round(primary[2] * 100) if primary else None
        return percent if percent in SCALES else None

    def effective_scale(self, state):
        # Existing desktops can keep working until the new compositor is loaded.
        return self.scale_percent if state[3].get('native-takeover-scale-supported') else 100

    def describe(self):
        """Live facts for the dashboard; small enough for one STATUS line."""
        state = self.state()
        size = state[3].get('native-takeover-size')
        return {'viewport': list(size) if size else None,
                'scale_percent': self.viewport_scale(state) or self.scale_percent,
                'remote_only_monitors': len(virtual_connectors(state)),
                'scale_supported': bool(state[3].get('native-takeover-scale-supported')),
                'multi_monitor_supported': bool(state[3].get('native-takeover-multimonitor-supported'))}

    def set_scale(self, percent):
        percent = validate_scale(percent)
        state = self.state()
        if not state[3].get('native-takeover-scale-supported'):
            raise RuntimeError('Remote scaling requires the new Mutter; log out and log in once')
        old = self.scale_percent
        self.scale_percent = percent
        try:
            if self.privacy_state() == 'remote':
                self.resize(*state[3]['native-takeover-size'], extras=self.extras,
                            scale_percent=percent)
        except Exception:
            self.scale_percent = old
            raise

    def state(self):
        return self.connection.call_sync(NAME, OBJECT, NAME, 'GetCurrentState',
                                         None, None, 0, 3000, None).unpack()

    def privacy_state(self):
        name = 'org.gnome.Mutter.NativeTakeover'
        return self.connection.call_sync(name, '/org/gnome/Mutter/NativeTakeover', name,
                                         'GetState', None, None, 0, 3000, None).unpack()[0]

    def apply(self, state, config, method=1, attempts=8):
        for attempt in range(attempts):
            # Never apply an old connector/mode to a different physical monitor.
            available = {spec[0]: (list(spec), {m[0] for m in modes})
                         for spec, modes, _ in state[1]}
            for logical in config['logical']:
                for connector, mode, _ in logical[5]:
                    if (connector not in available or
                            available[connector][0] != config['identities'][connector] or
                            mode not in available[connector][1]):
                        raise RuntimeError('Monitor changed; saved layout retained for recovery: ' + connector)
            try:
                self.connection.call_sync(NAME, OBJECT, NAME, 'ApplyMonitorsConfig',
                                          arguments(state[0], config, method), None, 0, 5000, None)
                return
            except GLib.Error as error:
                # Remote-only monitors appear and disappear asynchronously and
                # bump the serial; revalidate against a fresh state and retry.
                if 'stale information' not in error.message or attempt == attempts - 1:
                    raise
            time.sleep(.1)
            state = self.state()

    def enter(self):
        if self.privacy_state() != 'remote':
            raise RuntimeError('Single-screen switch requires the physical privacy barrier')
        if self.journal.exists():
            raise RuntimeError('Previous monitor layout must be restored before a new lease')
        state = self.state()
        saved = snapshot(state)
        if sum(len(m[5]) for m in saved['logical']) <= 1:
            return
        primary = next(m for m in saved['logical'] if m[4])
        current_mode = next(mode for spec, modes, _ in state[1] if spec[0] == primary[5][0][0]
                            for mode in modes if mode[6].get('is-current'))
        width, height = current_mode[1:3]
        if primary[3] % 2:
            width, height = height, width
        target, _ = remote_monitor(state, width, height, self.effective_scale(state))
        self.apply(state, target, method=0)  # Validate before writing/changing anything.
        self.save(saved)
        self.apply(self.state(), target)
        actual = snapshot(self.state())
        if len(actual['logical']) != 1 or len(actual['logical'][0][5]) != len(saved['identities']):
            raise RuntimeError('Compositor did not preserve every physical output in the mirrored layout')
        print(json.dumps({'event': 'native-mirrored-monitors',
                          'connectors': [o[0] for o in target['logical'][0][5]]}), flush=True)

    def save(self, saved):
        self.journal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(saved, f)
            f.flush()
            os.fsync(f.fileno())

    def resize(self, width, height, extras=(), scale_percent=None):
        """Resize the viewport; without a scale the live viewport's is kept."""
        if self.privacy_state() != 'remote':
            raise RuntimeError('Resize requires an active remote privacy barrier')
        self.extras = tuple(tuple(e) for e in extras)
        state = self.state()
        if scale_percent is not None:
            self.scale_percent = validate_scale(scale_percent)
        else:
            self.scale_percent = self.viewport_scale(state) or self.scale_percent
        target, actual = remote_monitor(state, width, height, self.effective_scale(state), self.extras)
        current = snapshot(state)
        if (target['logical'] != current['logical'] or
                target['properties'] != current['properties']):
            self.apply(state, target, method=0)
            if not self.journal.exists():
                self.save(current)
            self.apply(self.state(), target)
        applied = self.state()
        if 'native-takeover-size' in target['properties']:
            if tuple(applied[3].get('native-takeover-size', ())) != actual:
                raise RuntimeError('Compositor did not apply the requested remote viewport')
        primary = next(m for m in applied[2] if m[4])
        print(json.dumps({'event': 'native-adaptive-resolution', 'requested': [width, height],
                          'selected': list(actual), 'exact': actual == (width, height),
                          'scale_percent': round(primary[2] * 100),
                          'extra_monitors_requested': len(self.extras),
                          'remote_only_monitors': len(applied[2]) - len(snapshot(applied)['logical'])}),
              flush=True)

    def restore(self):
        if not self.journal.exists():
            return
        saved = json.loads(self.journal.read_text())
        # The marker tells Mutter this is the lease owner giving the viewport
        # up, not GNOME Settings changing the scale of the remote desktop.
        release = dict(saved['properties'], **{'native-takeover-release': True})
        self.apply(self.state(), dict(saved, properties=release))
        self.journal.unlink()
        print(json.dumps({'event': 'native-monitor-layout-restored'}), flush=True)


def recover(journal=None):
    journal = Path(journal) if journal else Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'ubuntu-native-takeover/monitor-layout.json'
    if not journal.exists():
        return
    layout = MonitorLayout(Gio.bus_get_sync(Gio.BusType.SESSION, None), journal)
    # After a broker crash Mutter first revokes capture/input and confirms lock.
    for _ in range(40):
        if layout.privacy_state() == 'local':
            layout.restore()
            return
        time.sleep(.2)
    raise RuntimeError('Privacy recovery still pending; saved monitor layout retained')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--journal', help='Override runtime journal path for isolated testing')
    recover(parser.parse_args().journal)
