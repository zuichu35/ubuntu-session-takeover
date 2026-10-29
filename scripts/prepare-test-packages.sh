#!/usr/bin/env bash
# Downloads/extracts test executables locally. Does not install system packages.
set -euo pipefail
takeover_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
mkdir -p "$takeover_root/vendor/ubuntu-mutter" "$takeover_root/vendor/ubuntu-freerdp"
cd "$takeover_root/vendor/ubuntu-mutter"
apt-get download mutter=50.1-0ubuntu2.2
for takeover_package in ./*.deb; do dpkg-deb -x "$takeover_package" root; done
cd "$takeover_root/vendor/ubuntu-freerdp"
apt-get download freerdp-x11=3.31.0+dfsg-0ubuntu0.26.04.1 libfreerdp-client3-3=3.31.0+dfsg-0ubuntu0.26.04.1
for takeover_package in ./*.deb; do dpkg-deb -x "$takeover_package" root; done
