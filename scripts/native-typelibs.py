#!/usr/bin/env python3
"""Copy matching system GI metadata into the private compositor's library path.

Native Mutter reorders Shell's default GI repository before JavaScript starts.
The copied metadata is unchanged; the launcher checks its original ABI hashes.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
lib = ROOT / 'build/native-prefix/lib/x86_64-linux-gnu'
destination = lib / 'mutter-18'
destination.mkdir(parents=True, exist_ok=True)
for namespace, library in [('Meta', 'libmutter-18.so.0'), ('Clutter', 'libmutter-clutter-18.so.0'),
                           ('Cogl', 'libmutter-cogl-18.so.0'), ('Mtk', 'libmutter-mtk-18.so.0')]:
    original = Path('/usr/lib/x86_64-linux-gnu/mutter-18') / f'{namespace}-18.typelib'
    raw = original.read_bytes()
    if raw.count(library.encode() + b'\0') != 1:
        raise RuntimeError('Unexpected typelib library metadata: ' + str(original))
    (destination / original.name).write_bytes(raw)
print('Private GI bindings prepared for Meta, Clutter, Cogl and Mtk.')
