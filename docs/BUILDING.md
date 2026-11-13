# Source Build and Verification Boundaries

This is experimental source for Ubuntu 26.04 **amd64, GNOME/Mutter 50, libmutter ABI 18**.
The build scripts are not a general-purpose installer. Do not run install, migration or
`tests/live*.py` directly on a machine with important desktop work. A separate build machine/VM of the same version is recommended; the apt commands on this page change that build machine's
packages, while the build steps themselves write only the project's `vendor/`, `build/` and `dist/`.

Source tests and target integration CI are separate: the latter builds and tests
Mutter/GRD and the real system Shell from the pinned upstream sources in an isolated Ubuntu 26.04 container, and its result is whatever the Actions of the corresponding commit say. **Real GDM/PAM login and
Windows/macOS client acceptance remain separate from CI**.
A pinned source commit does not mean the apt dependencies, the compiler and the output binaries are bit-for-bit reproducible.

## 1. Base tools and dependencies

You need the package sources and `deb-src` for Ubuntu 26.04 amd64. Prepare the dependencies on a dedicated build machine, for example:

```bash
sudo apt-get update
sudo apt-get install git build-essential pkgconf meson ninja-build python3 \
  python3-gi python3-cairo python3-pil gettext python3-docutils \
  gnome-shell libmutter-18-0 gir1.2-mutter-18 \
  libpam0g-dev glslc spirv-tools
sudo apt-get build-dep mutter gnome-remote-desktop
```

Upstream requires Meson ≥1.5 (Mutter), GLib ≥2.81.1, FreeRDP ≥3.22 and libei ≥1.3.901.
The default GRD RDP build also needs the CUDA/NVENC **headers** (Ubuntu `libffmpeg-nvenc-dev`) and
development dependencies for FreeRDP/WinPR, PipeWire, FUSE3, TPM2, Secret, Polkit, Vulkan and AAC;
`glslc` and `spirv-opt` are used to generate shaders. Use the distribution's `build-dep` to resolve the full transitive dependencies;
do not assume that the few development packages mentioned in the README are enough. NVIDIA drivers are not required for pure-CPU isolated tests.

Isolated integration tests additionally need `dbus-daemon pipewire wireplumber xvfb xdotool openssl
iproute2 util-linux gir1.2-gtk-4.0`, and a working FreeRDP X11 client. The test client location is
currently still `vendor/ubuntu-freerdp/root/usr/bin/xfreerdp`.
`scripts/prepare-test-packages.sh` downloads the pinned FreeRDP version and the old Mutter test programs from apt;
these exact versions may not be obtainable in the current mirror, and they must not be silently swapped for another version while keeping the old acceptance conclusions.
This is not a prerequisite for the basic Python unit tests.

`scripts/prepare-native-deps.py` is kept as a historical helper for unpacking local `.deb` files: it downloads only the dependencies
missing on the current host, depends on the current apt state, and **is not a complete or locked bootstrap**. This page takes the path of installing the full dependencies on a dedicated build
machine; `vendor/native-deps` and `vendor/grd-deps` may be empty, and pkg-config
still queries the standard system paths. Do not upload an old machine's dependency cache to the repository.

## 2. Fetching the pinned upstream sources

```bash
python3 scripts/fetch-upstreams.py --plan
python3 scripts/fetch-upstreams.py
python3 scripts/fetch-upstreams.py --check
```

| Directory | Upstream | Pinned commit |
|---|---|---|
| `vendor/mutter` | `https://github.com/GNOME/mutter.git`, 50.1 | `8e9bb806da4a6a4fc87aa4a481b228dba6735cbd` |
| `vendor/gnome-remote-desktop` | `https://github.com/GNOME/gnome-remote-desktop.git`, 50.2 | `60423c896a54e3eacb65bd93167e91c1ce5e648c` |
| `vendor/mutter/subprojects/gvdb` | `https://gitlab.gnome.org/GNOME/gvdb.git` | `b54bc5da25127ef416858a3ad92e57159ff565b3` |

GVDB is a pinned sub-project of Mutter; it is fetched ahead of time here to avoid an implicit download during the Meson stage.
This project's build disables docs/profiler and does not need their optional gi-docgen/sysprof sub-projects. GDM is a system runtime
dependency and no private GDM is compiled, so GDM source is not downloaded.

The fetch script creates only missing directories. An existing checkout whose revision or origin does not match is rejected; a matching one
is left as is, including a dirty working tree, and a successful `--check` only proves that the baseline and origin match, **not that the modifications
have been reviewed**. The script never runs reset/clean. The optional `--object-cache /path/to/existing/vendor`
only reads local Git objects, does not copy their uncommitted changes and does not write the cache; it is suitable for offline patch checks.
`--plan` does not touch the network or create directories; `--check` does not touch the network or modify the working tree.

## 3. Applying patches and building the development version

```bash
python3 scripts/patch-native.py
python3 scripts/patch-grd.py
python3 scripts/build-native.py
python3 scripts/build-grd.py --plan
python3 scripts/build-grd.py
python3 scripts/record-native-abi.py --plan
python3 scripts/record-native-abi.py
python3 scripts/record-native-abi.py --check
```

The patch scripts verify the full Git revision and reject existing edits they cannot recognize. The Mutter baseline is upstream 50.1,
plus the four hashed Ubuntu patches in `patches/ubuntu-50.1-0ubuntu2.4/series.json`, plus this project's
takeover changes; it is not Ubuntu's complete patch set. The generated `mutter-native-takeover.patch` **already includes**
those four patches and must not be applied again. After modifying `takeover/native-*.c/.h`, run the patch script again.

Development builds install into `build/native-prefix` and `build/grd-prefix` inside the project; do not use sudo.
GRD's sysusers/tmpfiles targets are also pinned to the private prefix; all Meson install targets are checked before installation,
and anything outside is rejected. The PAM helper is compiled from this repository's `takeover/local-auth.c` and is not copied from a deployed program.
The system Shell's ABI 18 typelibs are copied into the private prefix, so a matching installed system package is required.

The ABI record script reads only seven fixed public system files and by default writes the in-project
`build/native-system-abi.json`; it does not query credentials, change configuration or start services. It is a diagnostic snapshot and
**not proof of ABI compatibility or of passing tests**. After a system update, re-evaluate, rebuild and re-verify; refreshing only the
hashes must not be taken as compatibility. The launcher additionally requires the compatibility profile, the final artifact manifest and the actual target test receipt to match;
without verification it will not try to load the private Mutter. See [Compatibility mechanism](COMPATIBILITY.md).

The development prefix, once compiled, contains absolute resource paths of the current build directory; it cannot be moved directly to `/opt` and cannot be used by a
GDM user without access to that HOME. The GRD used for GDM must be recompiled for the final `/opt` prefix,
in particular for PTX, shaders, schemas and other runtime resources.

## 4. Unit tests and isolated tests

```bash
python3 -m unittest discover -s tests -p 'test_source_bootstrap.py' -v
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

The latter command includes tests that compile small C fixtures and requires the development dependencies to be complete; some historical packaging tests need
extra local fixtures. Real compositor/RDP tests need built binaries and a private network, D-Bus,
PipeWire and runtime directories. Do not run live tests directly or inject test state into the real seat0.

`tests/native_integration.py` and `tests/native_startup_lock.py` cover dual-lock state, timeout recovery,
wrong password, revocation after authentication, stale callbacks, remote reconnect, early GDM redirect and target wait.
`scripts/test-*-candidate.py` are the maintainers' isolated harnesses; some still require a pre-assembled test-opt
or a specific bundle and are not generic test entry points that work right after a clone. Passing headless fake authentication cannot replace acceptance with real
user PAM, a physical screen/keyboard/mouse and Windows App/macOS clients.

## 5. Maintainer candidate builds and packaging

The following plan mode only prints inputs/outputs and does not compile or deploy:

```bash
python3 scripts/build-startup-lock-candidate.py --plan
python3 scripts/build-greeter-fix.py --plan
python3 scripts/package-native.py --plan
```

`build-startup-lock-candidate.py` and `build-grd-greeter-candidate.py` use the fixed runtime prefix
`/opt/ubuntu-native-takeover-login-20261028/build/{native,grd}-prefix`, but install only into the DESTDIR under
`build/*/stage` and never write to the live `/opt`. The former compiles the local PAM helper directly, and the latter checks the
PTX/resource prefix and `/home` dependencies. Only after the build can `build-greeter-fix.py` assemble a
local bundle from the two stages; the locale and helper both come from the new build and do not depend on a personal HOME or an existing install.

These date-pinned migration tools target existing configuration/state and must not be used as first-time installers.
`build-handover-fix.py` still depends on the old bundle; the installer's state/manifest validation also needs an existing deployment.
`package-native.py` retains another historical `/opt/ubuntu-native-takeover` **experimental user-service package**,
which is not the same as the new GDM login-migration package. This round only checked the source code and lightweight paths, and has not re-verified its full build and install.
Do not publish a locally generated `.deb` or the old dist; if binaries are published in future, the complete corresponding source
(including GVDB), all third-party notices, runtime dependencies and target-platform acceptance must be re-checked.
