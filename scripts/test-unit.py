#!/usr/bin/python3
"""Run the source-level suite without a desktop or installed private runtime."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 'org.gnome.desktop.remote-desktop.rdp.headless'
DCONF_TEST = ('test_greeter_config.GreeterConfigTests.'
              'test_compiled_file_database_is_read_only_and_does_not_create_user_database')


def dconf_unavailable_reason():
    missing = [tool for tool in ('dconf', 'gsettings') if not shutil.which(tool)]
    if missing:
        return 'optional dconf integration requires: ' + ', '.join(missing)
    # Match the test: it deliberately uses installed schemas, not an inherited
    # development schema directory. This command does not read or change keys.
    environment = dict(os.environ)
    environment.pop('GSETTINGS_SCHEMA_DIR', None)
    try:
        result = subprocess.run(['gsettings', 'list-keys', SCHEMA], env=environment,
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return 'optional dconf integration could not inspect the installed GRD headless schema'
    if result.returncode:
        return 'optional dconf integration requires the installed GRD headless schema: ' + SCHEMA
    missing_keys = {'enable', 'port', 'negotiate-port'} - set(result.stdout.splitlines())
    if missing_keys:
        return 'optional dconf integration schema lacks keys: ' + ', '.join(sorted(missing_keys))
    return None


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quiet', action='store_true', help='show only test progress and summary')
    args = parser.parse_args()
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern='test_*.py')
    reason = dconf_unavailable_reason()
    if reason:
        for case in cases(suite):
            if case.id() == DCONF_TEST:
                method = getattr(type(case), case._testMethodName)
                setattr(type(case), case._testMethodName, unittest.skip(reason)(method))
                print('SKIP ' + DCONF_TEST + ': ' + reason, file=sys.stderr)
                break
    result = unittest.TextTestRunner(verbosity=1 if args.quiet else 2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
