# Experimental Packaging Tools

The current GitHub release contains source only, with no ready-to-install `.deb`, local runtime or credentials.
For the full usage boundaries see the [deployment notes](../docs/DEPLOYMENT.md), and for build dependencies the [build instructions](../docs/BUILDING.md).

`package-native.py` and `control.py` are kept to maintain the experimental single-user native desktop package.
This package uses `/opt/ubuntu-native-takeover`, a different path from the dated GDM login-migration bundle;
do not mix the two sets of install records, manifests, compile prefixes or rollback scripts.

## Packaging prerequisites

- Build artifacts matching the GNOME Shell / Mutter ABI, the complete runtime dependencies and the ABI diagnostic record already exist.
- Isolated integration tests have been run against the corresponding binaries, rather than borrowing old results from the development machine.
- The complete corresponding sources, sub-projects such as GVDB, and the licenses of all dependencies have been checked; the current source repository contains no offline dependency cache.
- Installation of the generated package, normal login, remote takeover and rollback have been verified on a dedicated test machine.

A plan that does not run the build can be viewed first:

```bash
python3 scripts/package-native.py --plan
```

The current `.deb` assembler produces development candidates only; assembling clears old compatibility receipts, and there is not yet a supported binary
acceptance, re-packaging and release process, so a generated `.deb` must not be treated as an enableable version.
The setup in `control.py` first requires the root-managed final artifacts and the target test receipts to all match, and only then interactively sets the RDP credentials and the local TLS
material. This entry point is not a finished GDM login installation plan for a new machine. The repository provides no ready-made account or password,
and such material must not be added to Git.

Modifying Mutter requires the user to save work and log in again/reboot to load it; installation preparation and the desktop switch should be done separately.
Disconnecting RDP keeps the applications, while logging out closes them. Local reclaim uses a separate password-authentication screen,
and Ctrl+Alt+Delete does not bypass password authentication.
