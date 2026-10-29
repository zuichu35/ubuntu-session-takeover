#!/usr/bin/env python3
"""Build startup-lock recovery into a separate DESTDIR, never into live /opt."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_candidate', ROOT / 'scripts/build-greeter-candidate.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
build.WORK = ROOT / 'build/native-startup-lock-20261028'
build.STAGE = build.WORK / 'stage'
if __name__ == '__main__':
    build.main()
