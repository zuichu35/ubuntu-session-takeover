# TODO

## To verify on the host

- [ ] **GDM greeter at 100% remote scale** (commit 29dbf65). Deploy
      `dist/runtime-multimon-20261111b`, log out, connect over RDP and check
      that the login screen is shown 1:1 instead of the 150% default. The
      greeter broker log should report `"scale_percent": 100`
      (`journalctl -b | grep native-adaptive-resolution`, unit
      `ubuntu-native-greeter.service`).

- [ ] **Client scale and GNOME Settings scale.** `dist/runtime-scale-20261113`
      was installed on 2026-11-13, but no remote session has run on it yet
      (the journal shows none after the install). Install the current bundle
      with `sudo python3 scripts/deploy-runtime.py`, log in again, then:
      connect from Windows at 125% and check that the broker log reports
      `"scale_percent": 125` for both the greeter and the desktop; change the
      scale in GNOME Settings → Displays during the session. The session must
      stay connected and the remote desktop must change its scale (before
      this change the compositor crashed with SIGSEGV in the screen-cast
      read-back).

- [ ] **Other changes from GNOME Settings → Displays during a session.**
      Orientation, a second monitor and disabling a monitor are rejected in
      CI. Resolution and refresh rate could not be tested there (the
      headless monitor has one mode); on the host they must be rejected with
      "The remote session owns the display layout; only its scale can be
      changed" and leave the session connected.

- [ ] **UST dashboard.** Run
      `python3 /opt/ubuntu-native-takeover/scripts/remote-settings.py
      --install-shortcut` as the desktop user (no sudo). "UST" must appear in
      the application menu, `ust --status` must print the live state, the
      scale buttons must act on a running remote session, and the autostart
      entry must read "Native Desktop Remote Takeover" instead of the
      Chinese name the first setup wrote. Tested so far only with unit tests
      in a temporary home directory.

## Open

- [ ] Windows mstsc "Use all my monitors" on real monitors: CI covers only the
      FreeRDP X11 client on two RandR monitors. First manual test (2026-11-11)
      worked: two monitors, correct geometry and resolution.
- [ ] A foreign `RecordVirtual` screen cast (for example a local Miracast sink)
      during a lease must stay behind the physical barrier; there is no
      integration test for it yet.
- [ ] Remote frames can stop updating after a particular series of scale
      changes: 175%, a one second pause, then 125%, 200%, 150% in quick
      succession left the client with a stale image in about one of three
      runs of `tests/native_integration.py --adaptive --exact-viewport
      --remote-scale` (headless CI, 2026-11-13). The application still
      receives input. It happens with the broker `SCALE` command on the
      runtime built before the client-scale change too, so it is not caused
      by that change. Not yet seen on the host.
- [ ] `tests/native_integration.py --adaptive --exact-viewport` without
      `--remote-scale` fails with "Mouse input outside the physical 1280x720
      area failed", also on the previous runtime.
- [ ] With a fractional remote scale that a virtual monitor does not support,
      extra monitors fall back to a row to the right of the viewport instead of
      the client positions.
