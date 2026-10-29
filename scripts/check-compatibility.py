#!/usr/bin/env python3
"""Inspect compatibility without loading Shell, libraries, buses or services."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from takeover.compatibility import inspect_candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path, default=ROOT)
    parser.add_argument('--profile', help='Default: trusted build manifest profile_id')
    parser.add_argument('--development-owner', action='store_true',
                        help='Explicit build/fixture inspection only; never authorizes production environment')
    parser.add_argument('--plan', action='store_true', help='Print inputs and policy without reading runtime files')
    args = parser.parse_args()
    if args.plan:
        print(json.dumps({'runtime_root': str(args.runtime_root), 'profile_id': args.profile,
                          'required_owner': os.getuid() if args.development_owner else 0,
                          'read_only': True, 'loads_shell_or_libraries': False}, indent=2))
        return 0
    report = inspect_candidate(args.runtime_root, profile_id=args.profile,
                               required_owner=os.getuid() if args.development_owner else 0)
    print(json.dumps(report, indent=2))
    return {'verified': 0, 'needs-validation': 2, 'incompatible': 3}[report['status']]


if __name__ == '__main__':
    raise SystemExit(main())
