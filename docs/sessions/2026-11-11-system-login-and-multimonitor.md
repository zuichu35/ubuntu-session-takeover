# Session notes 2026-11-11: system login entry and multi-monitor

Compacted record of a working session on the development host
(Ubuntu 26.04, GNOME 50.1, two iiyama PL2792Q on DP-1/DP-2, DP-2 disabled
locally). Credentials, addresses beyond the LAN and personal data are omitted.

## 1. Remote connection failed after the English-translation reinstall

Symptom: after reinstall + reboot, the Windows client could not connect.

Findings:
- The translation itself was not the cause. The host had only the per-user
  setup (`control.py setup`), which listens on 3392 only after a local login.
  After the reboot nobody was logged in locally, so 3392 was closed.
- The client fell back to the stock system GRD on 3389 (Ubuntu remote login).
  That creates a headless, seat-less session; the per-user Shell wrapper then
  refused it (`startup_policy.py`: exactly one physical seat0 Wayland session
  required) and the session ended.
- The system login mode (gateway 3392 at the GDM greeter) had never been
  installed on this host, and the existing `install-*-fix.py` scripts only
  upgrade a historical install.

Fix:
- New `scripts/install-system-login.py` (commit c40719d): first-time install
  of the final login state for `/opt/ubuntu-native-takeover` (gateway unit,
  read-only greeter GRD settings and credentials, Shell launcher, startup
  guard and greeter units, backend override), with a disable-login.py
  compatible manifest and its own `rollback`.
- Deployed with sudo; stock system GRD on 3389 disabled
  (`grdctl --system rdp disable`, `systemctl disable --now
  gnome-remote-desktop.service`); GDM restarted.
- "Unexpected server authentication certificate" seen once in mstsc: the
  served certificate was unchanged and matched its key; later attempts on
  3392 succeeded. A second machine kept connecting to 3389, which has a
  different certificate. The user-level GNOME Desktop Sharing on 3389 is
  still enabled in the session and bypasses the privacy barrier; disable it
  with `grdctl rdp disable` if unused.

## 2. Remote scale

The remote desktop used the 150% default (`remote_settings.DEFAULT_SCALE`).
Set to 100% with `scripts/remote-settings.py --scale 100` (stored in
`~/.config/ubuntu-native-takeover/remote-settings.json`, applies live).
Display Settings during a remote session show only the temporary viewport,
never the physical layout.

## 3. Multi-monitor (option B), commit 2ea0c94

Before: one remote screen only. GRD capped the monitor count at 1, Mutter
accepted exactly one viewport logical monitor at 0,0, and the broker protocol
carried one size.

Options considered:
- `mstsc /span`: works without changes, but GNOME sees one wide monitor.
- A: one physical output per client monitor (needs as many physical outputs,
  lights up DP-2).
- B (chosen): primary client monitor = existing physical-backed viewport;
  other client monitors = Mutter virtual monitors created by GRD.
- All-virtual: cleaner long-term, but changes lease-end/revoke ordering.

Design:
- Mutter: during a lease, `ensure_configured` keeps the viewport and adds
  remote-only virtual monitors (`meta_monitor_config_manager_native_extend`)
  at broker hints (`native-takeover-extra`); unplugged passengers are dropped;
  `GetCurrentState` reports `native-takeover-remote-only`.
- Only virtual monitors tagged by the leased GRD
  (`org.gnome.native-takeover.remote-slot` PipeWire tag) are remote-only;
  other screen casts and headless test outputs stay behind the barrier.
- Privacy painting (black barrier, local password UI, "all outputs presented
  black") applies only to physical views (`meta_native_privacy_view_is_physical`,
  fails closed).
- GRD: hybrid layout, primary = connector stream of the viewport, others =
  RecordVirtual; `REMOTE/LAYOUT w h [x y w h]...` to the broker;
  `SupportMonitorLayoutPdu` enabled. The live viewport stream is kept across
  layout changes and re-recorded only after Mutter closes it.
- Broker: LAYOUT protocol; invalid layouts keep the lease; layout apply
  retries on stale configuration serials (virtual monitors change the serial
  asynchronously, which broke restore on disconnect in CI).
- `multi_monitor` setting (default on) enabled only if the running
  compositor reports `native-takeover-multimonitor-supported`; greeter stays
  single-screen.

Bugs found and fixed while testing:
- Viewport stayed black next to a virtual monitor: upstream monitor streams
  had no `is_cursor_metadata_valid`, so cursor-only frames triggered by the
  continuously repainting virtual view kept every full frame "too early".
- An independent review found: any RecordVirtual treated as remote-only
  (privacy), equal-size monitor mapping, a resize freeze in the hybrid state
  machine, missing compositor gating, passenger unplug, INT32_MIN parsing,
  and lease-ending parse errors; all fixed.

Testing:
- New integration scenario `tests/native_integration.py --multimon`
  (Xvfb with two RandR monitors via `tests/x11_monitors.py`, FreeRDP
  `/multimon`); added to `scripts/ci-target.py` and required by the new
  profile `ubuntu-26.04-amd64-gnome-50.1-host-multimon-20261111`.
- Full target CI passed 6/6 locally in the target image, with the checkout
  mounted at `/opt/ubuntu-native-takeover` so resource paths match the
  deployment.
- Deployed `dist/runtime-multimon-20261111` with an atomic directory
  exchange (`renameat2 RENAME_EXCHANGE`); previous runtime kept as
  `/opt/ubuntu-native-takeover.prev-20261110`.
- Manual test with Windows mstsc "Use all my monitors": both monitors at the
  correct resolution and geometry after login.

## 4. Greeter scale, commit 29dbf65

After deployment the first (login) screen appeared at 150%: the greeter runs
as a dynamic gdm account and cannot read the user's 100% setting. The
greeter broker now always uses 100%. Package
`dist/runtime-multimon-20261111b`; verification on the host is tracked in
`TODO.md`.

## Deployment notes

- Builds run in the target image as the host user with the checkout mounted
  at `/opt/ubuntu-native-takeover`; the host lacks PAM headers, so
  `test_local_auth` and `test_native_input` error in the host unit run.
- Changing patches or runtime code needs a new profile hash/receipt: run
  `scripts/ci-target.py` in the image, assemble the runtime (compiled
  prefixes, receipts, CI evidence, profiles, scripts, takeover;
  `package-files.json` lists regular files), install as root and swap
  atomically. A new Mutter loads only after logout/login or reboot.
