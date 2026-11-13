#!/usr/bin/python3
"""Install a dist/runtime-* bundle as /opt/ubuntu-native-takeover (run as root).

    sudo python3 scripts/deploy-runtime.py [BUNDLE_DIRECTORY]
    sudo python3 scripts/deploy-runtime.py --rollback

The running session keeps the old files it has open; the replaced runtime is
kept beside the new one as /opt/ubuntu-native-takeover.prev-<time>, and
--rollback swaps the most recent of those back. Log out and in afterwards.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

DEFAULT_SOURCE = Path(__file__).resolve().parents[1] / 'dist/runtime-scale-20261113d'
LIVE = Path('/opt/ubuntu-native-takeover')
STAGE = Path('/opt/ubuntu-native-takeover.next')
RENAME_EXCHANGE = 2


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo')
    arguments = sys.argv[1:]
    if arguments == ['--rollback']:
        kept = previous_runtimes()
        if not kept:
            raise SystemExit(f'No {LIVE}.prev-* to roll back to')
        exchange(LIVE, kept[-1])
        print(f'Rolled back: {LIVE} is the runtime kept as {kept[-1].name}; that name now holds the '
              'one just replaced. Log out and in.')
        return
    if len(arguments) > 1 or any(argument.startswith('-') for argument in arguments):
        raise SystemExit(__doc__)
    source = Path(arguments[0]).resolve() if arguments else DEFAULT_SOURCE
    previous = LIVE.with_name(f"{LIVE.name}.prev-{time.strftime('%Y%m%d-%H%M%S')}")
    if not LIVE.is_dir() or STAGE.exists() or previous.exists():
        raise SystemExit(f'Unexpected state: need {LIVE}, and neither {STAGE} nor {previous}')
    listing_text = (source / 'package-files.json').read_text()
    live_listing = LIVE / 'package-files.json'
    if live_listing.exists() and live_listing.read_text() == listing_text:
        raise SystemExit(f'{source.name} is already installed as {LIVE}; nothing to do.')
    listing = json.loads(listing_text)
    shutil.copytree(source, STAGE, symlinks=True)
    for directory, _, names in os.walk(STAGE):
        os.lchown(directory, 0, 0)
        os.chmod(directory, 0o755)
        for name in names:
            path = os.path.join(directory, name)
            os.lchown(path, 0, 0)
            if not os.path.islink(path):
                # Same modes as the runtime installed before: nothing group-writable.
                os.chmod(path, 0o755 if os.stat(path).st_mode & 0o111 else 0o644)
    installed = {str(p.relative_to(STAGE)) for p in STAGE.rglob('*')
                 if p.is_file() and not p.is_symlink()} - {'package-files.json'}
    expected = set(listing) - {'package-files.json'}
    wrong = [name for name in expected & installed if digest(STAGE / name) != listing[name]]
    if installed != expected or wrong:
        shutil.rmtree(STAGE)
        raise SystemExit(f'Copy does not match package-files.json: {sorted(installed ^ expected)[:5]} {wrong[:5]}')
    exchange(LIVE, STAGE)
    STAGE.rename(previous)
    print(f'Installed {source.name} as {LIVE}; previous runtime kept as {previous}.')
    print('Log out and log in again (or reboot) to load the new Mutter.')
    print(f'Rollback: sudo python3 {Path(__file__).resolve()} --rollback')


def previous_runtimes():
    """Kept runtimes, oldest first (names sort by their time stamp)."""
    return sorted(path for path in LIVE.parent.glob(LIVE.name + '.prev-*') if path.is_dir())


def exchange(first, second):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renameat2(-100, os.fsencode(first), -100, os.fsencode(second), RENAME_EXCHANGE):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(first))


if __name__ == '__main__':
    main()
