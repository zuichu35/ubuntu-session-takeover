# Native Session Takeover and Login Handover

## Logged-in desktop

After GRD completes RDP authentication it requests an exclusive lease from the broker. Mutter isolates the raw physical input, clears key/button state that has already been delivered, waits for every physical output to confirm a privacy frame, and only then allows remote input and off-screen capture.

The physical password screen is drawn into the physical framebuffer and is not an actor in the desktop scene, so it never enters remote capture. After the correct local password passes PAM, the remote session is closed synchronously and capture and input are revoked, and only then is physical access restored. A plain remote disconnect still requires confirming that both GNOME ScreenSaver and logind `LockedHint` are locked.

## Login after boot or logout

1. The system gateway forwards to 3394 through a validated greeter endpoint. The dynamic greeter identity is confirmed through the NSS primary group and the actual logind session, and does not depend on a fixed UID or a username prefix.
2. The greeter lease obtained after RDP authentication first creates a root-managed startup-protection marker, and only then allows input to the actual GDM login screen.
3. GDM/PAM creates the physical user session. The new Mutter keeps privacy protection from the first frame; a separate guard service and supervisor coordinate lock confirmation without blocking Shell startup from completing.
4. When the gateway sees a new physical session for the configured user, it notifies the still-living greeter as early as possible to send an RDP redirect. The client reconnects through 3392, and waits, within bounds, for the desktop endpoint to become ready.
5. Once the desktop endpoint is ready, RDP authentication and takeover are performed again. Even if the source greeter has already exited, the old TLS stream is never re-attached to the desktop.

GDM may stop the greeter on its own, and it cannot be assumed to wait for a custom backend. Client reconnect/handshake timeouts remain part of live acceptance.

## Privacy states

| State | Meaning |
|---|---|
| `local` | Locally usable, no current remote lease |
| `boot-guard` | New session is protected from the first frame; lock confirmation has not started |
| `startup-locking` | ScreenSaver and LockedHint are being checked independently |
| `ready-guarded` | Both locks confirmed; physical protection is kept while waiting for an authenticated takeover |
| `startup-failed` | Startup confirmation timed out; protection is kept and a local password recovery entry is shown |
| `blanking` | Authenticated takeover is waiting for physical privacy-frame confirmation |
| `remote` | Remote lease is in effect |
| `locking` | Lock recovery after an ordinary remote session ended |

`GetStatus` reports whether each of the two lock states is known, their values, the request state, elapsed time, reason, session path and local authentication state. Unknown and false must not be conflated.

If the startup lock is not completed within 30 seconds the state becomes `startup-failed`; low-frequency background retries can still recover it, and a password typed locally is not cleared by retries. Correct local authentication first forbids all remote access, then tries to release the Shell lock, waits at most 10 seconds, and restores local input. At that point the root gateway independently verifies the queried user, the running Shell PID, the session, the authentication marker and the remote-forbidden state before clearing startup protection; a boolean claim submitted by a client is not authentication evidence.

Asynchronous state observation uses generation numbers to cancel stale results, and the local PAM callback is also bound to the current authentication child process. A new lease cannot be reclaimed by a late result from a previous authentication.

## Failure handling

The supervisor waits only for explicit startup states; it does not take over `remote`, `blanking` or an ordinary `locking` belonging to another controller. When the gateway is briefly unavailable the current backend is kept and re-registered; if a child process exits, failure is reported. Gateway queries, handshake buffers, the number of pending connections and the stop procedure are all bounded, and an old session that is logging out does not block a new greeter.

Display-layout restore records belong to the user's runtime directory, and temporary changes are not written to the permanent display configuration. When a monitor's identity changes the record is kept and reported, so old parameters are never applied to a different device.

## Remote display and scale

GRD sends the broker one line per layout: `REMOTE|LAYOUT|RESIZE w h [x y w h]... [S<percent>]`. The first size is the viewport, each further group one extra client monitor, and the optional last field the desktop scale the RDP client announced (`DesktopScaleFactor`, taken from the primary monitor on a display-control update). The broker rounds it to 100, 125, 150, 175 or 200% and applies it when the lease starts and whenever the announced value changes; an unchanged value is ignored, so a scale chosen by hand survives resizing the client window. Without the field the saved setting is used (the greeter: 100%).

`SCALE <percent>` on the same socket changes the scale of a running session and saves it as the fallback; `STATUS` returns one JSON line with the lease state plus `viewport`, `scale_percent`, `remote_only_monitors`, `client_scale` and what the running compositor supports. The UST dashboard (`takeover/remote_settings_ui.py`) uses only these two commands.

While a lease holds the viewport, Mutter treats an `ApplyMonitorsConfig` call that does not come from the broker (GNOME Settings, `gdctl`) as a request about the remote desktop: with exactly one logical monitor, and the same monitors and modes as now, its scale is applied to the viewport as a temporary configuration (quarter steps, 1.0 to 2.0); anything else is rejected with an error. The broker marks its own restore with `native-takeover-release`, so giving the viewport up is never mistaken for such a request. Screen-cast read-back skips a frame whose buffer no longer matches the stream size, which is what crashed the compositor on a scale change before.

Early shared-desktop modules such as `takeover/broker.py` and `workspace.py`, and some test helper files, are kept for historical regression and source reference; the current native 3392 data path does not use the old local RDP viewer.
