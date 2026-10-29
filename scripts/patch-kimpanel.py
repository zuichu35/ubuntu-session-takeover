#!/usr/bin/python3
"""Apply the reviewed coordinate fix to Input Method Panel 92, idempotently.

Pass an isolated copy for tests. This never disables or restarts the extension.
"""
import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('extension_dir', type=Path)
args = parser.parse_args()
path = args.extension_dir / 'panel.js'
source = path.read_text()
if '// Native takeover: validated stage rectangle.' not in source:
    old = '''        x = rect.x;
        y = rect.y;
        w = rect.w;
        h = rect.h;'''
    new = '''        // Native takeover: validated stage rectangle.
        // Mtk.Rectangle exposes width/height, not w/h. Undefined dimensions
        // made the cursor actor invalid and broke BoxPointer positioning.
        if (!monitor) {
            monitor = Main.layoutManager.primaryMonitor;
        }
        x = Number.isFinite(rect.x) ? rect.x : (monitor?.x ?? 0);
        y = Number.isFinite(rect.y) ? rect.y : (monitor?.y ?? 0);
        w = Number.isFinite(rect.width) ? Math.max(1, rect.width) : 1;
        h = Number.isFinite(rect.height) ? Math.max(1, rect.height) : 1;
        if (monitor) {
            y = Math.max(monitor.y, y);
        }'''
    assert source.count(old) == 1, 'Unsupported kimpanel panel.js; refusing to overwrite'
    source = source.replace(old, new)
    source = source.replace('shellScale / kimpanel.scale', 'shellScale / (kimpanel.scale > 0 ? kimpanel.scale : 1)')
    path.write_text(source)

path = args.extension_dir / 'extension.js'
source = path.read_text()
if '// Native takeover: refresh after display scale/layout changes.' not in source:
    old = '''        this.addToShell();'''
    new = '''        this.addToShell();
        // Native takeover: refresh after display scale/layout changes.
        this._monitorSignal = Main.layoutManager.connect('monitors-changed', () => {
            if (!this._isDestroyed)
                this.updateInputPanel();
        });'''
    assert source.count(old) == 1
    source = source.replace(old, new)
    old = '''        this.settings.disconnect(this.verticalSignal);'''
    assert source.count(old) == 1
    source = source.replace(old, '''        Main.layoutManager.disconnect(this._monitorSignal);
        this.settings.disconnect(this.verticalSignal);''')
    old = '''            this.x = value[0];
            this.y = value[1];'''
    assert source.count(old) == 1
    source = source.replace(old, '''            changed = changed || this.relative || this.scale != 1;
            this.relative = false;
            this.scale = 1;
            this.x = value[0];
            this.y = value[1];''')
    path.write_text(source)
print('Kimpanel coordinate fix prepared:', args.extension_dir)
