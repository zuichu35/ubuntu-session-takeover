# Deployment and Maintenance Boundaries

What is currently published is experimental source. A complete initial deployment procedure for a new machine has not been turned into a supported one-click installer, and this repository has no installable Release binary attachments.

First read the [build instructions](BUILDING.md), build in a test environment that matches the GNOME/Mutter ABI, and run the [layered tests](TESTING.md). Build output, ABI records, TLS and RDP credentials must be produced by the target machine itself and not copied from the development machine.

## Tool categories

| Tool | Scope of use |
|---|---|
| `check-native-health.py` | Read-only by default; `--tls` performs a protocol probe only when idle and cannot prove a desktop login succeeded |
| `check-compatibility.py`, `native-shell.py --check` | Read-only checks of the exact compatibility profile, ABI, file ownership, final artifacts and target test receipts |
| `install-native.py` | The historical user-directory installer is disabled; it does not meet the root-managed requirements of the new startup gate |
| `finalize-compatibility-bundle.py` | Seals the two compatibility receipts after the final artifact tests pass; must not change existing package files at the same time |
| `package-native.py`, `packaging/control.py` | Experimental packaging / interactive setup tools; dependencies, test evidence and target layout must be reviewed first |
| `install-greeter-fix.py`, `install-greeter-startup-fix.py`, `install-handover-fix.py` | Upgrades of an existing install with an exact old manifest and original backups; they must refuse to run if the prerequisite state is missing |
| `build-greeter-fix.py`, `build-handover-fix.py` | Bundle assemblers for the corresponding maintenance bundles; the latter depends on the old bundle and cannot be used to bootstrap an empty directory |
| `deploy-runtime.py` | Host maintenance (root): copies a `dist/runtime-*` bundle beside `/opt/ubuntu-native-takeover`, checks every file against `package-files.json`, exchanges the two directories atomically and keeps the replaced one as `.prev-<time>`; `--rollback` swaps the most recent one back. Changes no unit or setting; the new Mutter loads at the next login |
| `remote-settings.py` (menu entry "UST", command `ust`) | Runs as the desktop user, never with sudo: dashboard and `--status`, `--scale`, `--multi-monitor`; `--install-shortcut` adds the launcher to an existing installation and renames the entries an earlier setup wrote |
| `disable-native.py`, `disable-login.py` | Explicit rollback entry points that stop or switch the related RDP backends; read the specific script and the install record first |
| `reload-native-idle.py` | Waits for the backend to be idle and then reloads RDP; cannot update a running Mutter |
| `tests/live_power_save.py` | Live-hardware test requiring explicit confirmation that changes display power and lease state; not part of default CI |

The dated `/opt` prefix and the old-manifest anchors in the maintenance scripts belong to specific upgrade protocols. Changing only the path without recompiling the resource prefix leaves GDM unable to read its resources. `--headless` GRD is used here to select separate settings/credentials; it does not mean a second user desktop is created.

## Preserving current work

A running Wayland compositor cannot be replaced directly while keeping all of its clients. Preparing or atomically publishing the files for the next start is a separate phase from restarting/logging out; the user must save their work and then log in again normally or reboot to load them.

By default the native backend does not stop the services of the old shared desktop. The related migration actions run only when `--retire-legacy-services` is explicitly chosen.

A change in the system ABI fingerprint is used for diagnostics; a private start must also pass the exact compatibility profile and target test receipt checks.
An unverified combination falls back to the system Shell only when it is confirmed that there is no privacy guard and no existing compositor, and the native backend
exits for that session; if a guard exists or the state is unclear, startup is refused. The final packaged artifact must be re-verified, and the
installer refuses to activate a new launcher that lacks a valid receipt. See [Compatibility mechanism](COMPATIBILITY.md).

Private mode refuses any `LD_PRELOAD` or `LD_AUDIT` that is still present after this project's paths are cleaned up, so that preloaded libraries cannot take the same-named
libraries ahead of Mutter. The launcher keeps the distribution's original `ExecStart` and wrapper arguments, but it cannot stop
an arbitrary wrapper from setting loader environment again after it executes. Additional wrappers (including remote-desktop products that inject libraries)
must be verified in the target environment against the actual command and environment; the current default CI does not prove that these wrappers work.
Do not re-inject variables through a wrapper to bypass the startup check. A start with a privacy guard in particular must not depend on an unverified wrapper.

## Live acceptance

After saving work and rebooting, stay at GDM first and do not log in locally; connect to 3392 from the actual client, verify the account on the remote Ubuntu login screen, and confirm desktop display and input. Record whether the redirect is automatic or a manual reconnect is needed.

Then check that disconnect/reconnect keeps the same applications, that local password reclaim revokes the remote, and that logging out and back in after saving work works. A usable local recovery screen after lock confirmation fails only proves the recovery path works; it does not mean remote login acceptance has passed.

Read-only status checks:

```bash
gdbus call --session --dest org.gnome.Mutter.NativeTakeover \
  --object-path /org/gnome/Mutter/NativeTakeover \
  --method org.gnome.Mutter.NativeTakeover.GetStatus
systemctl --user status ubuntu-native-takeover.service
journalctl --user -b -u ubuntu-native-takeover.service \
  -u ubuntu-native-login-guard.service
```

When troubleshooting, keep the logs of the failed start; a listening 3392, a TLS handshake, or a normal state after a new local login cannot by themselves show that the earlier boot handover has been fixed.
