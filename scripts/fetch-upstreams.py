#!/usr/bin/env python3
"""Fetch pinned upstream source into vendor; never reset an existing checkout."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
UPSTREAMS = (
    ('mutter', 'https://github.com/GNOME/mutter.git',
     '8e9bb806da4a6a4fc87aa4a481b228dba6735cbd'),
    ('gnome-remote-desktop', 'https://github.com/GNOME/gnome-remote-desktop.git',
     '60423c896a54e3eacb65bd93167e91c1ce5e648c'),
    ('mutter/subprojects/gvdb', 'https://gitlab.gnome.org/GNOME/gvdb.git',
     'b54bc5da25127ef416858a3ad92e57159ff565b3'),
)


def git(path, *args):
    return subprocess.check_output(['git', '--no-optional-locks', '-C', str(path), *args], text=True).strip()


def inspect(path, url, revision):
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError('Refusing non-directory or symlink checkout: ' + str(path))
    if Path(git(path, 'rev-parse', '--show-toplevel')).resolve() != path.resolve():
        raise RuntimeError('Expected a separate Git checkout: ' + str(path))
    if git(path, 'rev-parse', 'HEAD') != revision:
        raise RuntimeError('Existing checkout has another revision; left unchanged: ' + str(path))
    if git(path, 'remote', 'get-url', 'origin') != url:
        raise RuntimeError('Existing checkout has another origin; left unchanged: ' + str(path))
    return {'path': str(path), 'revision': revision,
            'dirty': bool(git(path, 'status', '--porcelain')), 'action': 'kept unchanged'}


def fetch(vendor, cache=None, check=False):
    results = []
    for relative, url, revision in UPSTREAMS:
        path = vendor / relative
        for parent in [vendor, *path.relative_to(vendor).parents]:
            candidate = parent if parent == vendor else vendor / parent
            if candidate.is_symlink():
                raise RuntimeError('Refusing symlink checkout ancestor: ' + str(candidate))
        if path.exists() or path.is_symlink():
            results.append(inspect(path, url, revision))
            continue
        if check:
            raise RuntimeError('Missing upstream checkout: ' + str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        # Fetch objects only; a cache's uncommitted working files are never copied.
        source = url if cache is None else str(cache / relative)
        if cache is not None:
            git(Path(source), 'cat-file', '-e', revision + '^{commit}')
        temporary = Path(tempfile.mkdtemp(prefix='.' + path.name + '-', dir=path.parent))
        try:
            subprocess.run(['git', 'init', '--quiet', str(temporary)], check=True)
            git(temporary, 'remote', 'add', 'origin', url)
            git(temporary, '-c', 'protocol.file.allow=always', 'fetch', '--quiet',
                '--depth=1', source, revision)
            git(temporary, 'checkout', '--quiet', '--detach', revision)
            inspect(temporary, url, revision)
            temporary.rename(path)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        created = inspect(path, url, revision)
        created['action'] = 'fetched pinned objects'
        results.append(created)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--plan', action='store_true', help='Print pinned inputs; no network or writes')
    modes.add_argument('--check', action='store_true', help='Check existing revisions/origins; no writes')
    parser.add_argument('--object-cache', type=Path,
                        help='Optional read-only vendor tree supplying pinned Git objects')
    args = parser.parse_args()
    if args.plan:
        result = [{'path': str(ROOT / 'vendor' / name), 'url': url, 'revision': revision}
                  for name, url, revision in UPSTREAMS]
    else:
        result = fetch(ROOT / 'vendor', args.object_cache, args.check)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
