#!/usr/bin/env python3
"""Record final candidate artifacts/source/target facts; never create a passing test receipt."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from takeover.compatibility import (DEFAULT_PROFILE, MANIFEST, CompatibilityError, build_manifest,
                                    collect_snapshot, load_profile, sha256, trusted_read)


def source_facts(root, profile, owner):
    facts = {}
    for name, relative in (('mutter', 'mutter'), ('gnome-remote-desktop', 'gnome-remote-desktop'),
                           ('gvdb', 'mutter/subprojects/gvdb')):
        path = root / 'vendor' / relative
        facts[name] = subprocess.check_output(['git', '--no-optional-locks', '-C', str(path),
                                               'rev-parse', 'HEAD'], text=True, timeout=5).strip()
    facts['patches'] = {name: sha256(trusted_read(root / name, root, owner))
                        for name in profile['sources']['patches']}
    for component, source in (('native', 'mutter'), ('grd', 'gnome-remote-desktop')):
        recorded = json.loads(trusted_read(root / f'build/{component}-patch-hashes.json', root, owner))
        if not isinstance(recorded, dict) or not recorded:
            raise CompatibilityError('Missing patched source hash record')
        for relative, expected in recorded.items():
            path = root / 'vendor' / source / relative
            if sha256(trusted_read(path, root / 'vendor' / source, owner)) != expected:
                raise CompatibilityError('Patched source has changed: ' + relative)
    for name in ('native-privacy.c', 'native-privacy.h', 'native-local-lock.c', 'native-local-lock.h'):
        local = trusted_read(root / 'takeover' / name, root, owner)
        generated = trusted_read(root / 'vendor/mutter/src/backends' / ('meta-' + name), root, owner)
        if local != generated:
            raise CompatibilityError('Patch source is stale: ' + name)
    return facts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path, default=ROOT)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--compiled-prefix', type=Path)
    parser.add_argument('--profile', default=DEFAULT_PROFILE)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--development-owner', action='store_true', help='Explicit unprivileged build inspection')
    parser.add_argument('--plan', action='store_true', help='Print paths without reading artifacts or writing')
    args = parser.parse_args()
    runtime, source = args.runtime_root.absolute(), args.source_root.absolute()
    prefix = args.compiled_prefix or runtime
    output = args.output or runtime / MANIFEST
    if args.plan:
        print(json.dumps({'runtime_root': str(runtime), 'source_root': str(source),
                          'runtime_prefix': str(prefix), 'output': str(output),
                          'profile_id': args.profile, 'creates_validation_receipt': False}, indent=2))
        return
    owner = os.getuid() if args.development_owner else 0
    profile, digest = load_profile(runtime, args.profile, owner)
    facts = collect_snapshot(runtime, profile, required_owner=owner,
                             profile_sha256=digest, runtime_prefix=prefix)
    result = build_manifest(profile, facts, runtime_prefix=prefix,
                            sources=source_facts(source, profile, owner))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=output.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(json.dumps(result, indent=2) + '\n')
            handle.flush()
            temporary.chmod(0o644)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
    print(str(output))


if __name__ == '__main__':
    main()
