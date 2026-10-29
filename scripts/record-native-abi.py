#!/usr/bin/env python3
"""Read public Shell/Mutter files and record diagnostic hashes; never change services."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_FILES = (
    '/usr/lib/x86_64-linux-gnu/libmutter-18.so.0', '/usr/bin/gnome-shell',
    '/usr/lib/gnome-shell/libshell-18.so',
    *('/usr/lib/x86_64-linux-gnu/mutter-18/' + name + '-18.typelib'
      for name in ('Meta', 'Clutter', 'Cogl', 'Mtk')),
)


def snapshot(paths=SYSTEM_FILES):
    return {str(path): hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--plan', action='store_true', help='List public inputs and output; no writes')
    modes.add_argument('--check', action='store_true', help='Compare existing record; no writes')
    parser.add_argument('--output', type=Path, default=ROOT / 'build/native-system-abi.json')
    args = parser.parse_args()
    if args.plan:
        print(json.dumps({'inputs': SYSTEM_FILES, 'output': str(args.output),
                          'proves_compatibility': False}, indent=2))
        return
    observed = snapshot()
    if args.check:
        if json.loads(args.output.read_text()) != observed:
            raise SystemExit('System files differ from the recorded diagnostic ABI snapshot')
        print('Diagnostic ABI snapshot matches; this is not a runtime compatibility test.')
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=args.output.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(json.dumps(observed, indent=2) + '\n')
            handle.flush()
            temporary.replace(args.output)
        finally:
            temporary.unlink(missing_ok=True)
    print(str(args.output))


if __name__ == '__main__':
    main()
