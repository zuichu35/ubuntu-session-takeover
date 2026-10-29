#!/usr/bin/env python3
"""Start a verified private Shell, or a proven-safe local system fallback.

Preserve the distribution's ExecStart and wrapper argv. Never replace a running
compositor, unlock a session, remove its privacy guard, or restart a service.
"""
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from takeover import startup_policy as policy


def candidate_report():
    try:
        from takeover.compatibility import inspect_candidate
        return inspect_candidate(ROOT, required_owner=0)
    except Exception as error:
        return {'schema': 1, 'status': 'incompatible', 'paths': {},
                'reasons': ['Candidate verification unavailable: ' + type(error).__name__]}


def private_environment(report):
    from takeover.compatibility import private_environment as verified_environment
    return verified_environment(report)


def refusal(reason):
    return ('Native takeover startup refused: ' + str(reason) +
            '. No guard was cleared and no session was unlocked. '
            'Use a TTY or SSH to inspect the Shell service journal and follow '
            'docs/DEPLOYMENT.md recovery instructions; do not delete the boot guard to bypass privacy.')


def main(args=None):
    args = list(sys.argv[1:] if args is None else args)
    if args == ['--check']:
        report = candidate_report()
        report.update(startup_policy='verified-or-safe-local-fallback',
                      artifact_hash_mismatch_blocks_private_startup=True,
                      system_hash_change_is_diagnostic=True)
        print(json.dumps(report))
        return
    if args[:1] == ['--']:
        args.pop(0)
    if not args:
        raise SystemExit('Original GNOME Shell command required')
    fd = None
    try:
        fd = policy.acquire_launch_lock()
        context = policy.inspect_startup()
        # GDM can briefly create its dynamic NSS record before logind publishes
        # the session. Hints only request retries; they cannot grant its role.
        if '--mode=gdm' in args and policy.is_gdm_group_member(os.getuid()):
            for _ in range(2):
                if context.get('session'):
                    break
                time.sleep(.25)
                context = policy.inspect_startup()
        report = candidate_report()
        mode = policy.choose_mode(report, context)
        # Candidate hashing may take time. Recheck actual owners and guard
        # before publishing a receipt or executing the saved command.
        latest = policy.inspect_startup()
        if (latest.get('session') != context.get('session') or latest.get('role') != context.get('role') or
                policy.choose_mode(report, latest) != mode):
            raise policy.StartupRefused('Session ownership changed during startup verification')
        env = policy.clean_environment(os.environ, ROOT, report)
        if mode == 'private':
            selected = private_environment(report)
            # Keep unrelated wrapper paths, after the verified private paths.
            for key in ('LD_LIBRARY_PATH', 'GI_TYPELIB_PATH'):
                if env.get(key) and selected.get(key):
                    selected[key] += ':' + env[key]
            env.update(selected)
            env['MUTTER_NATIVE_TAKEOVER'] = '1'
            if latest['role'] == 'greeter':
                env['MUTTER_NATIVE_GREETER'] = '1'
            elif latest['guard'] == 'present':
                env['MUTTER_NATIVE_BOOT_PRIVACY'] = '1'
        else:
            print('Native takeover: candidate is not verified; starting the original system Shell '
                  'for this local session with the native backend disabled. Reasons: ' +
                  '; '.join(str(v) for v in report.get('reasons', [])), file=sys.stderr, flush=True)
        env['UBUNTU_NATIVE_SESSION_MODE'] = mode
        policy.write_launch_receipt(mode, latest)
        if policy.guard_state() != latest['guard']:
            raise policy.StartupRefused('Startup privacy guard changed immediately before exec')
        try:
            os.execvpe(args[0], args, env)
        except OSError as error:
            raise policy.StartupRefused(f'Failed to execute original wrapper: {error}') from error
    except (RuntimeError, OSError, ValueError) as error:
        raise SystemExit(refusal(error)) from error
    finally:
        if fd is not None:
            os.close(fd)


if __name__ == '__main__':
    main()
