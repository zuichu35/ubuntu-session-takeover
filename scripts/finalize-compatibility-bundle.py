#!/usr/bin/python3
"""Seal only newly validated compatibility metadata into a candidate bundle.

No runtime build, installation, ownership change or service operation occurs.
All previously packaged bytes/links must remain unchanged. This does not issue
a validation receipt: the target tests must already have produced it.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from takeover.compatibility import inspect_candidate, MANIFEST, VALIDATION


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(payload):
    files, links = {}, {}
    for path in sorted(payload.rglob('*')):
        relative = path.relative_to(payload).as_posix()
        if path.is_symlink():
            target = path.readlink()
            if target.is_absolute() or not path.resolve(strict=True).is_relative_to(payload):
                raise RuntimeError('Unsafe bundle symlink: ' + relative)
            links[relative] = str(target)
        elif path.is_file():
            files[relative] = digest(path)
        elif not path.is_dir():
            raise RuntimeError('Unsupported bundle file: ' + relative)
    return {'version': 1, 'files': files, 'symlinks': links}


def finalize(bundle):
    bundle = Path(bundle).resolve()
    payload = bundle / 'payload'
    anchor = bundle / 'payload-manifest.json'
    if payload.is_symlink() or anchor.is_symlink():
        raise RuntimeError('Bundle payload and manifest must not be symlinks')
    before = json.loads(anchor.read_text())
    after = inventory(payload)
    allowed = {MANIFEST, VALIDATION}
    if (before.get('version') != 1 or before.get('symlinks', {}) != after['symlinks'] or
            set(after['files']) != set(before['files']) | allowed or
            any(after['files'].get(p) != h for p, h in before['files'].items())):
        raise RuntimeError('Finalization only adds compatibility receipts; packaged files changed')
    manifest = json.loads((payload / MANIFEST).read_text())
    report = inspect_candidate(payload, required_owner=os.getuid(), runtime_prefix=manifest['runtime_prefix'])
    if report.get('status') != 'verified':
        raise RuntimeError('Target validation has not passed: ' + json.dumps(report))
    # Recheck the complete outer tree after inspection, before sealing it.
    if inventory(payload) != after:
        raise RuntimeError('Bundle changed during finalization')
    with tempfile.NamedTemporaryFile(mode='w', dir=bundle, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(after, indent=2) + '\n')
            stream.flush()
            temporary.chmod(0o644)
            temporary.replace(anchor)
        finally:
            temporary.unlink(missing_ok=True)
    return {'bundle': str(bundle), 'manifest_sha256': digest(anchor),
            'profile_id': report['profile_id'], 'deployment_authorized': False,
            'next_step': 'Installer rechecks the root-managed staged payload against the target host'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    args = parser.parse_args()
    print(json.dumps(finalize(args.bundle), indent=2))


if __name__ == '__main__':
    main()
