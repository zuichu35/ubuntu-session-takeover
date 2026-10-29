FROM ubuntu:resolute-20261024.1

ARG CI_UID=1001
ARG CI_GID=1001
ENV DEBIAN_FRONTEND=noninteractive
# This image is used only by the disposable hosted CI job. Installing desktop
# packages must not start system services in the build container.
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod 755 /usr/sbin/policy-rc.d \
 && sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/ubuntu.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git build-essential pkgconf meson ninja-build gettext python3-docutils \
      python3 python3-gi python3-cairo python3-pil \
      gnome-shell libmutter-18-0 gir1.2-mutter-18 gnome-remote-desktop \
      libpam0g-dev glslc spirv-tools dbus-daemon pipewire wireplumber \
      xvfb xdotool openssl iproute2 util-linux gir1.2-gtk-4.0 freerdp-x11 \
      libgl1-mesa-dri libegl-mesa0 mesa-utils \
 && apt-get build-dep -y mutter gnome-remote-desktop \
 && mkdir -p /run/systemd/seats \
 && groupadd --gid "$CI_GID" target-ci \
 && useradd --uid "$CI_UID" --gid "$CI_GID" --create-home target-ci \
 && rm -rf /var/lib/apt/lists/*
ENV HOME=/home/target-ci
USER target-ci
WORKDIR /workspace
CMD ["/usr/bin/python3", "scripts/ci-target.py"]
