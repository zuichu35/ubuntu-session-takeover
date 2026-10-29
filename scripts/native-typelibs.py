#!/usr/bin/env python3
"""Copy the explicit profile's unchanged system typelibs to a private build."""
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from takeover.compatibility import DEFAULT_PROFILE, load_profile


def copy_typelibs(root, profile, system_root=Path('/')):
    destination_root = Path(root).resolve()
    for artifact in profile['artifacts']:
        if artifact['kind'] != 'typelib':
            continue
        destination = destination_root / artifact['path']
        if not destination.resolve().is_relative_to(destination_root) or destination.is_symlink():
            raise RuntimeError('Typelib destination escapes the private build')
        system_path = Path('/usr') / profile['paths']['library_subdir'] / profile['paths']['mutter_subdir'] / destination.name
        if str(system_path) not in profile['system_files']:
            raise RuntimeError('Profile does not declare the source typelib: ' + str(system_path))
        original = Path(system_root) / str(system_path).lstrip('/')
        raw = original.read_bytes()
        if raw.count(artifact['library'].encode() + b'\0') != 1:
            raise RuntimeError('Unexpected typelib library metadata: ' + str(original))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default=DEFAULT_PROFILE)
    args = parser.parse_args()
    # This prepares developer build files, not production launch authorization.
    profile, _ = load_profile(ROOT, args.profile, required_owner=os.getuid())
    copy_typelibs(ROOT, profile)
    print('Private GI metadata copied using the explicit compatibility profile.')


if __name__ == '__main__':
    main()
