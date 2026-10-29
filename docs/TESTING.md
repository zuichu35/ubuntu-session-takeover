# Testing and acceptance

The default suite tests source-level behavior without starting a desktop,
installing the private runtime, changing system services, or using real
credentials. Run it as an ordinary user from a Linux checkout:

```sh
sudo apt-get install python3 python3-gi build-essential pkg-config \
  libglib2.0-dev libinput-dev libpam0g-dev
/usr/bin/python3 scripts/test-unit.py
```

Use the distribution's `/usr/bin/python3` so that `python3-gi` is available.
Python 3.12 or newer is recommended. The GitHub workflow uses Ubuntu 24.04
and its system Python 3.12. Test execution does not require sudo; only the
dependency installation above does. The suite uses temporary directories,
temporary sockets and child processes. C fixtures compile extracted production
logic with fake input or PAM interfaces. They do not exercise a real password
or physical input device. No `vendor/`, `build/` or installed `/opt` runtime is
needed for this suite.

The runner discovers `tests/test_*.py`. One optional test compiles and reads a
real dconf database. It requires `dconf`, `gsettings`, and an installed
`org.gnome.desktop.remote-desktop.rdp.headless` schema with the tested keys.
If these are unavailable, the runner reports that exact test as **skipped**;
the other greeter configuration tests still run. Missing Python modules,
compilers or C headers are errors, not blanket skips. Kernel restrictions can
also cause the existing nondumpable-process tests to report a skip. Always
record both the number run and the skipped count.

`CPATH=/path/to/development/headers /usr/bin/python3 scripts/test-unit.py` can
be used when headers were extracted separately. Such a run verifies the source
checkout against those headers; it is not evidence that a clean operating
system already has every dependency.

## Validation matrix

| Layer | What it checks | Requirements and limits |
| --- | --- | --- |
| Default source suite | Gateway and startup state machines, authorization checks, process shutdown, monitor calculations, installation/rollback fixtures, input/PAM boundary fixtures | Ordinary Linux user; dependencies above; mocked system interfaces and temporary resources |
| Optional dconf integration | Locked settings database and absence of a user database write | dconf tools and the GRD headless schema; no daemon or desktop required |
| Target CI | Fresh pinned Mutter/GRD build, real target GNOME Shell and private-library mappings, startup privacy, local recovery, RDP input and accepted/rejected handover | Ubuntu 26.04 container on a hosted runner; ordinary UID, no capabilities, no host buses or physical GPU; exact profile required |
| Private compositor integration | Real patched Mutter/GRD/FreeRDP, capture/input behavior, independent lock-property failures and early handover | Matching built runtime and test tools, private D-Bus, CPU rendering, private network/mount/PID namespaces |
| Privilege-boundary probe | Root-to-user status query, actual bus-owner PID checks and inherited `NoNewPrivs` | Root only inside a disposable isolated environment; fake services on a private bus |
| Physical acceptance | Cold boot, actual GDM lifecycle and PAM, logind lock state, GPU/display/input, Windows/macOS client behavior | Dedicated test machine and explicit operator-controlled boot/login; **pending for the published startup-recovery changes** |

## Isolated integration

The historical candidate harnesses are separate from the default source CI. Use a disposable VM or dedicated
test machine with a matching Ubuntu/GNOME build. Prepare the pinned upstream
sources, runtime and dependencies first; see the build instructions. The
candidate launchers expect their staged payload and runtime paths to exist.
They are not standalone source-only tests.

The entry points are:

```sh
/usr/bin/python3 scripts/test-startup-lock-candidate.py
/usr/bin/python3 scripts/test-gateway-handover-candidate.py
```

The launchers invoke sudo for private mount, network and PID namespaces. They
hide physical GPU device nodes, use software rendering, and run the test
desktop on private buses. Inspect and adapt their staging paths before use.
Do not replace them with a command that attaches to the current session bus,
or run a compositor probe against an active work desktop. The namespace-only
payloads include `tests/native_startup_lock.py` and `tests/native_integration.py`.
The latter also supports `--shell` for a real GNOME Shell integration, when a
matching Shell/typelib environment has been prepared.

Fault-injected ScreenSaver/logind properties and a test authentication helper
verify failure handling. They do not reproduce real GDM PAM or establish that
a physical cold boot succeeds. A real FreeRDP connection in this environment
also does not establish Windows/macOS client acceptance.

## Target-version CI

`.github/workflows/target.yml` runs on pushes, pull requests and explicit manual
runs. It builds `ci/target.Dockerfile` on an Ubuntu 24.04 hosted runner, then runs
`scripts/ci-target.py` as an ordinary user inside the Ubuntu 26.04 container.
The test container drops all capabilities and has `no-new-privileges`; it has
its own network namespace and no host session/system bus, DRM or NVIDIA device
mounts. It never uses `--privileged` or the host network. Dependency installation
happens only in the disposable build image, with service startup disabled.

The job fetches exact source commits, applies patches, builds both runtimes,
records the candidate manifest and runs five required scenarios: startup lock
faults, real GNOME Shell with boot guard, desktop RDP, successful gateway handover
and rejected handover authentication. ScreenShield is the real target Shell;
logind/GDM and authentication are isolated test fixtures. The runner verifies
actual Shell process library mappings and checks tested file hashes again after
all cases. Failure in any case prevents a passing validation receipt.

The Actions summary includes the deliberately public target validation receipt;
no local development-machine artifacts or journal exports are uploaded. The
receipt records the exact package tuple and its limitations. A later archive
update that changes that tuple requires a new profile and another successful
run; it does not silently broaden support. The CI profile is currently
`ubuntu-26.04-amd64-gnome-50.1-updates-20261029`.

Run status and test acceptance are separate: a workflow file existing in the
repository is not evidence of a successful build. Check the Actions run for
the exact released commit. A headless target receipt cannot authorize a
relocated binary or establish physical cold-boot acceptance.

## Manual physical acceptance

Keep this outside ordinary CI. Save work, retain an accessible rollback path,
and perform the required reboot or logout deliberately. Verify the full path:

1. A cold boot exposes the physical greeter through the gateway.
2. A wrong password is rejected without revealing a desktop.
3. A correct login starts the intended existing physical session, completes
   both ScreenSaver and logind lock confirmation, and hands the client over.
4. Remote capture/input is available only behind the physical privacy barrier.
5. Wrong local recovery passwords preserve that barrier. Correct local PAM
   recovery revokes remote capture/input before restoring physical access.
6. Disconnect/reconnect, local recovery, display power transitions and layout
   restoration preserve the applications and behave correctly on actual
   monitors and input devices.

`tests/live_power_save.py` is an operator-controlled live probe, not a CI test;
read its acknowledgement and precondition requirements before use. A listener,
TLS handshake, successful package installation or source-test result alone is
not end-to-end acceptance.

## Evidence and publication

Earlier local validation recorded 185 passing source tests, followed by 10
targeted installer tests after an additional regression was added. Those are
historical runs, not a claim that every later checkout has passed. Use the
current runner output and the GitHub Actions result for the released commit.
Actual cold-boot GDM/PAM and physical GPU/display acceptance of the startup
recovery candidate remain pending.

Do not publish development-machine `artifacts/`, runtime bundles, journal
exports, screenshots, account information or credential files. The source CI
uploads none of them. Publish only deliberately sanitized summaries or output
generated by the disposable CI job itself.
