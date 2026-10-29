#!/usr/bin/python3
"""Stage compatibility policy, invalidating receipts for rebuilt payloads.

This only prepares a candidate. Record and test the final, unmodified payload
before an installer may activate its launcher.
"""
import argparse
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
FILES = ('takeover/compatibility.py', 'takeover/startup_policy.py',
         'scripts/check-compatibility.py')
RECEIPTS = ('build/compatibility-manifest.json', 'build/compatibility-validation.json')


def stage(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or source.is_relative_to(destination):
        raise RuntimeError('Compatibility staging requires a separate payload directory')
    profiles = sorted((source / 'compat/profiles').glob('*.json'))
    if not profiles:
        raise RuntimeError('No compatibility profiles to package')
    for relative in [*FILES, *(p.relative_to(source) for p in profiles)]:
        target = destination / relative
        if target.is_symlink() or not target.resolve().is_relative_to(destination):
            raise RuntimeError('Compatibility staging path escapes payload')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    # Native binaries were just rebuilt, stripped or relocated. A receipt from
    # a previous tree cannot authorize this payload even if the profile matches.
    for relative in RECEIPTS:
        target = destination / relative
        if not target.parent.resolve().is_relative_to(destination):
            raise RuntimeError('Receipt path escapes payload')
        target.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    stage(ROOT, args.destination)


if __name__ == '__main__':
    main()
