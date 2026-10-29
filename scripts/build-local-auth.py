#!/usr/bin/env python3
"""Build the unprivileged PAM helper into the private prefix."""
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
include = root / 'vendor/native-deps/root/usr/include'
destination = root / 'build/native-prefix/libexec/native-local-auth'
destination.parent.mkdir(parents=True, exist_ok=True)
temporary = destination.with_suffix('.new')
subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', '-fstack-protector-strong',
                '-D_FORTIFY_SOURCE=2', '-Wl,-z,relro,-z,now', '-I'+str(include),
                str(root / 'takeover/local-auth.c'), '-Wl,-l:libpam.so.0',
                '-o', str(temporary)], check=True)
temporary.chmod(0o755)
temporary.replace(destination)
