# Third-party sources and notices

New project code is licensed under GPL-2.0-or-later unless a file states otherwise. The GPL version 2 text is in [COPYING](COPYING). Existing copyright headers, individual file licenses and patch authorship remain applicable; this repository does not relicense every upstream component under one license.

## Pinned upstreams

| Component | Source revision | License/source information |
|---|---|---|
| Mutter 50.1 | [`8e9bb806da4a6a4fc87aa4a481b228dba6735cbd`](https://github.com/GNOME/mutter/tree/8e9bb806da4a6a4fc87aa4a481b228dba6735cbd) | The project declares GPL-2.0-or-later; bundled subdirectories and individual files can have other licenses. Retain their original notices. |
| GNOME Remote Desktop 50.2 | [`60423c896a54e3eacb65bd93167e91c1ce5e648c`](https://github.com/GNOME/gnome-remote-desktop/tree/60423c896a54e3eacb65bd93167e91c1ce5e648c) | Modified GRD session sources retain GPL-2.0-or-later headers. |
| GVDB | [`b54bc5da25127ef416858a3ad92e57159ff565b3`](https://gitlab.gnome.org/GNOME/gvdb/-/tree/b54bc5da25127ef416858a3ad92e57159ff565b3) | Mutter's pinned subproject; source files include LGPL-2.1-or-later notices. It is fetched separately, not vendored in this Git repository. |

The project modifies Mutter's native input, physical painting, monitor configuration and privacy lifecycle; it modifies GRD's authenticated lease, redirection and capture handling. Reproducible patch generators are included alongside the resulting patches. This repository does not modify GDM itself.

## Ubuntu patches

`patches/ubuntu-50.1-0ubuntu2.4/` retains four distribution patches, their original headers and a SHA-256 inventory. They were checked against the corresponding Ubuntu Mutter source package. Package sources and copyright information are available through the [Ubuntu Mutter source package](https://launchpad.net/ubuntu/+source/mutter/50.1-0ubuntu2.4).

`patches/mutter-native-takeover.patch` already includes these four changes. Apply the generated aggregate patch once, or run the generator against its pinned pristine Git revision; do not additionally apply the four patches a second time.

## Dependencies and binary distribution

FreeRDP, GLib, PipeWire, GNOME Shell, PAM and the platform's graphics/input stack are external dependencies. Their binaries, package archives, local credentials and source checkout caches are not included in this source publication. Review each dependency's notices and corresponding source requirements separately before distributing a binary package; the root COPYING file is not a substitute for that inventory.
