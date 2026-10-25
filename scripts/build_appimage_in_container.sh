#!/usr/bin/env bash
# Build the AppImage inside a container, as release.yml does.
#
#   scripts/build_appimage_in_container.sh [IMAGE]
#
# An AppImage runs where glibc is at least as new as on the system it was
# built on. The default image, AlmaLinux 9, has glibc 2.34: the AppImage runs
# on AlmaLinux, Rocky and CentOS Stream 9 and later, Ubuntu 22.04 and later,
# Debian 12 and later. Its Python 3.9, Tk and OpenSSL are maintained there
# until 2032.
#
# Needs docker or podman (CONTAINER=... picks one). The checkout is mounted
# read-only and copied inside the container; only the AppImage lands in dist/.
set -euo pipefail

inside() {
  . /etc/os-release
  echo "=== building on ${PRETTY_NAME}"
  case "${ID}" in
    almalinux|rocky|centos|rhel|fedora)
      dnf -y -q install python3 python3-tkinter file findutils which >/dev/null
      ;;
    debian|ubuntu)
      export DEBIAN_FRONTEND=noninteractive
      apt-get -qq update >/dev/null
      apt-get -qq install -y python3 python3-tk file curl ca-certificates >/dev/null
      ;;
    *)
      echo "error: no way known to install python3 and Tk on ${ID}" >&2
      return 1
      ;;
  esac
  mkdir /build
  tar -C /src --exclude=./.git --exclude=./dist --exclude=./.venv \
    --exclude=./.appimage_stage --exclude=./.zipapp_stage -cf - . | tar -C /build -xf -
  cd /build
  PYTHON=/usr/bin/python3 ./scripts/build_appimage.sh
  cp dist/Usenet_Archiver-*.AppImage /out/
  # Owned by whoever owns dist/ outside (docker runs as root here).
  chown --reference=/out /out/Usenet_Archiver-*.AppImage
}

if [[ "${1:-}" == "--inside" ]]; then
  inside
  exit
fi

IMAGE="${1:-docker.io/library/almalinux:9}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER="${CONTAINER:-}"
if [[ -z "${CONTAINER}" ]]; then
  if command -v docker >/dev/null 2>&1; then CONTAINER=docker; else CONTAINER=podman; fi
fi
mkdir -p "${ROOT}/dist"
rm -f "${ROOT}"/dist/Usenet_Archiver-*.AppImage
"${CONTAINER}" run --rm -v "${ROOT}:/src:ro,z" -v "${ROOT}/dist:/out:z" "${IMAGE}" \
  bash /src/scripts/build_appimage_in_container.sh --inside
ls -l "${ROOT}"/dist/Usenet_Archiver-*.AppImage
