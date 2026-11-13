# Session notes 2026-11-13: client scale, display settings and the UST dashboard

Compacted record of a working session on the development host (Ubuntu 26.04,
GNOME 50.1). Credentials, addresses and personal data are omitted.

## 1. The scale of the RDP client was not used

Symptom: a Windows client at 125% got the saved remote scale (100 or 150%).

Cause: GRD receives the client's `DesktopScaleFactor`, but the line it sent
to the broker carried only sizes, so the broker always used the saved value.

Fix:
- GRD appends ` S<percent>` to `REMOTE`, `LAYOUT` and `RESIZE`
  (`scripts/patch-grd.py`); display-control updates pass the primary
  monitor's scale.
- Broker: `requested_scale()` rounds 100..500 to the nearest of
  100/125/150/175/200. The announced scale wins at connect and whenever it
  changes; an unchanged value is ignored so a manual scale survives window
  resizes. The saved setting is only the fallback (greeter: 100%).
- Decision by the user: what the client sends has priority.

## 2. Changing the scale in GNOME Settings ended the session

Symptom: Displays → Scale during a remote session "froze" it and no
reconnect was possible.

Cause: not a freeze. The request replaced the viewport configuration, and
the screen-cast read-back then copied a frame into a buffer of the old size;
gnome-shell died with SIGSEGV, which ends the session and its applications.

Fix (`scripts/patch-native.py`):
- During a lease a foreign `ApplyMonitorsConfig` with one logical monitor and
  unchanged monitors/modes becomes a temporary scale change of the viewport.
- Any other foreign request is rejected: "The remote session owns the display
  layout; only its scale can be changed". Found while testing: a request that
  disabled a monitor was accepted before this check existed.
- The broker's own restore carries `native-takeover-release`.
- `record_to_buffer` refuses a buffer that does not match the stream size.
- `MonitorLayout.resize()` keeps the live viewport scale when none is given,
  so a window resize does not undo a scale chosen in GNOME Settings.

## 3. UST dashboard

`takeover/remote_settings_ui.py` became a GTK 4 dashboard: live state (2 s
refresh over the broker's `STATUS`), scale buttons, multi-monitor switch;
CLI `--status`, `--scale`, `--multi-monitor`, `--install-shortcut`. The
setup tools install the menu entry "UST" and `~/.local/bin/ust`.
`--install-shortcut` upgrades an existing installation, refuses to overwrite
files the install manifest does not know, and renames the autostart entry
and old settings shortcut that the original Chinese-language setup wrote.

## 4. Testing and packaging

- New CI scenario `scale` and profile
  `ubuntu-26.04-amd64-gnome-50.1-host-scale-20261113`; target CI 7/7 in the
  target image. Host unit run: 263 tests, 2 known environment errors (PAM
  headers missing).
- Bundles `dist/runtime-scale-20261113` to `...13d`; `...13d` is current.
  The first one was installed on the host; nothing newer, and no remote
  session has run on it yet. `scripts/deploy-runtime.py` installs a bundle
  and keeps the replaced runtime for `--rollback`.
- Not verified on the host: real mstsc at 125%, the real GNOME Settings
  panel, rejection of resolution/refresh changes, the dashboard on a live
  session. Tracked in `TODO.md`, with two pre-existing defects found on the
  way (stale frames after a particular scale sequence; `--adaptive
  --exact-viewport` without `--remote-scale`).
