# Compatibility profiles and safe startup

Compatibility is an explicit, tested combination of distribution packages,
Shell ELF linkage, private Mutter/GRD binaries, typelibs and runtime resources.
The GNOME major version alone does not determine the Mutter ABI. No profile
automatically authorizes a future GNOME or distribution update.

## Profiles and evidence

`compat/profiles/` currently contains two Ubuntu 26.04 amd64 candidates:

| Profile suffix | GNOME Shell package | Mutter / typelib package | Purpose |
| --- | --- | --- | --- |
| `gnome-50.1` | `50.1-0ubuntu1.2` | `50.1-0ubuntu2.4` | Recorded development baseline |
| `gnome-50.1-updates-20261029` | `50.1-0ubuntu1.4` | `50.1-0ubuntu2.5` | Updated target integration CI |

Both explicitly declare ABI 18 and the exact GRD/FreeRDP package versions in
their JSON. The private source remains pinned upstream Mutter 50.1 plus the
four declared Ubuntu 2.4 backports and project patches. The updated system
profile does not claim that the private build contains the complete Ubuntu
2.5 patch series. A profile is a candidate specification, not a passing result.

Three inputs must agree:

1. The profile declares exact target versions, SONAMEs, exported symbols,
   typelib library names, source revisions and patch checksums.
2. `build/compatibility-manifest.json` binds the complete private-prefix file
   and symlink inventory, required Python runtime code, Shell/libshell dynamic
   linkage and compiled resource prefix to the observed target and source baseline.
3. `build/compatibility-validation.json` binds successful required target
   cases and actual tested binaries to that manifest.

The check returns `verified`, `needs-validation` or `incompatible`. Missing
tests and changed package combinations need validation; missing libraries,
wrong ABI, corrupt artifacts or unsafe paths are incompatible. Changes to
system file fingerprints are diagnostic and do not by themselves establish
compatibility or incompatibility. Production also requires root ownership
and nonwritable ancestry for policy, evidence and runtime files.
Changes to the actual Shell/libshell dependency list or loader paths require
new validation even if the package version string stays the same. This static
inspection is not a full implementation of the operating system's dynamic loader.

Read-only checks do not load libraries, start a Shell, access credentials or
change services:

```sh
/usr/bin/python3 scripts/check-compatibility.py --plan
/usr/bin/python3 scripts/check-compatibility.py --runtime-root /path/to/runtime
/usr/bin/python3 scripts/native-shell.py --check
```

The checker exits 0 for verified, 2 for needs-validation and 3 for incompatible.
`--development-owner` allows explicit inspection of an ordinary user's build;
its report cannot authorize a production launch. The production launcher has
no environment-variable bypass for ownership or test validation.

## Startup and fallback

The launcher checks the real physical logind session, the startup privacy
guard and existing compositor owners before selecting a mode:

| Candidate / session | Action at the next Shell startup |
| --- | --- |
| Verified private runtime; known session; no existing compositor | Start the private runtime; preserve required startup privacy |
| Unverified or incompatible runtime; guard definitely absent; no compositor owner | Start the system Shell and leave native takeover inactive for that session |
| Privacy guard exists and private runtime is not verified | Refuse startup; retain the guard |
| Session, bus or guard state unknown; existing compositor owner | Refuse a second or unsafe launch |

Fallback preserves the original wrapper command and arguments, removes this
project's private loader/GI paths and flags, and records the selected mode in
`/run/user/<uid>/ubuntu-native-takeover/shell-launch.json`. The backend accepts
that record only when boot ID, session, PID, process start time and actual Shell
D-Bus owner agree. It exits normally only for a confirmed system fallback with
the guard still absent. A stale file cannot suppress error handling for a new
process. No fallback path clears a guard, authenticates a user, or restarts
the current desktop.

This fallback restores an ordinary local system desktop where safe. It does
not promise continued 3392 takeover after an incompatible GNOME upgrade.
If the private compositor actually fails after execution, that exit remains
a failure; the launcher does not retry a system Shell after an unobserved
partial startup. Replacing a running Wayland compositor would end its apps.

## Building and accepting an update

Use a separate test environment. Build the final prefix and finish stripping
or packaging before recording its manifest:

```sh
/usr/bin/python3 scripts/record-build-compatibility.py --plan
/usr/bin/python3 scripts/record-build-compatibility.py \
  --runtime-root /path/to/candidate --source-root /path/to/source \
  --compiled-prefix /final/runtime/prefix \
  --profile ubuntu-26.04-amd64-gnome-50.1-updates-20261029 \
  --development-owner
```

This command records facts only; it never writes a passing validation receipt.
The target CI runner produces one only after all required real binary tests
pass and their hashes remain unchanged. Moving or rebuilding its `/workspace`
development runtime does not authorize an `/opt` deployment. The final `/opt`
candidate must be tested at its compiled prefix in an isolated environment.

Packaging copies the compatibility policy and discards stale build/validation
receipts. After testing the final prefix, run
`python3 scripts/finalize-compatibility-bundle.py /path/to/bundle` to add the
two valid receipts to its outer payload manifest. This step rejects changes
to any previously packaged file and does not invent passing evidence. Do not
rerun the assembler after validation: that creates another unvalidated payload.
The historical `.deb` assembler produces a development candidate only; it has
no supported validation/finalization/release pipeline in this revision.
Maintainer installers refuse to activate a new-policy runtime until
its complete, root-managed staged payload passes inspection against the target
system. The historical package setup also requires this gate. The old
`install-native.py` per-user installer is disabled because it writes to a
user-owned runtime and relies on obsolete evidence paths. This repository still has no supported fresh-machine
installer or precompiled release.

To add a new version, add a separate exact profile, port the source as needed,
and run target CI for that profile. Do not broaden a version range or rewrite
hashes to make an existing receipt pass. GNOME ABI changes can require actual
C code changes; these checks detect unsupported combinations, not automatically
port them. Physical GDM/PAM, GPU/display/input and Windows/macOS acceptance
remain a separate operator-controlled test stage.
