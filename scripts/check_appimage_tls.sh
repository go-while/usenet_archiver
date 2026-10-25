#!/bin/bash
# Run a built AppImage over TLS inside other distributions' containers.
#
#   scripts/check_appimage_tls.sh APPIMAGE [IMAGE...]
#
# Default images: Fedora, Arch, openSUSE Tumbleweed, Debian, AlmaLinux 9 (the
# oldest glibc it must run on, 2.34) and Ubuntu 22.04. Needs go and
# docker (or podman; CONTAINER=... picks one); NNTPS_TESTSERVER=... names a
# prebuilt test server instead of go. No real server is contacted:
# e2e/cmd/nntps-testserver serves 5 articles on 127.0.0.1 with a certificate
# from a CA it makes at start.
#
# First, every symlink in the AppImage must point inside it. Then, in each
# container:
#   1. the CA is not trusted yet: the pull must fail on the certificate;
#   2. the CA is added to the system store (the distribution's own tool):
#      the pull must fetch all 5 articles.
# The AppImage bundles Ubuntu's OpenSSL, which looks for CA certificates in
# /usr/lib/ssl only; step 2 fails on any distribution without that path
# unless the client finds the system bundle itself.
set -euo pipefail

PORT="${PORT:-5630}"

inside() {
  export APPIMAGE_EXTRACT_AND_RUN=1
  . /etc/os-release
  echo "=== ${PRETTY_NAME}"
  case "${ID}" in
    debian|ubuntu)
      export DEBIAN_FRONTEND=noninteractive
      apt-get -qq update >/dev/null
      apt-get -qq install -y ca-certificates >/dev/null
      ;;
  esac
  cd "$(mktemp -d)"
  cp /work/app.AppImage ./ai
  chmod +x ./ai

  pull() {
    rm -rf out && mkdir out
    local rc=0
    (cd out && timeout 120 ../ai -c pull -s "localhost:${PORT}" --no-netrc \
      -g test.group -C 1 -D 1 -t 10 >log 2>&1) || rc=$?
    PULL_RC=${rc}
    PULL_N=0
    if [[ -f out/test.group.mbox ]]; then
      PULL_N="$(grep -c '^From ' out/test.group.mbox || true)"
    fi
  }

  pull
  if [[ ${PULL_RC} -eq 0 ]] || ! grep -q CERTIFICATE_VERIFY_FAILED out/log; then
    echo "FAIL: the CA is not trusted yet, but the pull did not fail on the certificate (exit ${PULL_RC})"
    tail -n 20 out/log
    return 1
  fi
  echo "ok: untrusted CA refused"

  case "${ID}" in
    fedora|rhel|centos|almalinux|rocky)
      cp /work/ca.pem /etc/pki/ca-trust/source/anchors/usenet-archiver-check.pem
      update-ca-trust
      ;;
    arch)
      cp /work/ca.pem /etc/ca-certificates/trust-source/anchors/usenet-archiver-check.crt
      update-ca-trust
      ;;
    opensuse*|sles)
      cp /work/ca.pem /etc/pki/trust/anchors/usenet-archiver-check.pem
      update-ca-certificates
      ;;
    debian|ubuntu)
      cp /work/ca.pem /usr/local/share/ca-certificates/usenet-archiver-check.crt
      update-ca-certificates >/dev/null
      ;;
    *)
      echo "FAIL: no way known to add a CA on ${ID}"
      return 1
      ;;
  esac

  pull
  if [[ ${PULL_RC} -ne 0 || ${PULL_N} -ne 5 ]]; then
    echo "FAIL: CA in the system store, but the pull got ${PULL_N} of 5 articles (exit ${PULL_RC})"
    tail -n 20 out/log
    return 1
  fi
  echo "ok: CA in the system store, 5 of 5 articles"
}

if [[ "${1:-}" == "--inside" ]]; then
  inside
  exit
fi

if [[ $# -lt 1 || ! -f "$1" ]]; then
  echo "usage: $0 APPIMAGE [IMAGE...]" >&2
  exit 2
fi
APPIMAGE="$(readlink -f "$1")"
shift
IMAGES=("$@")
if [[ ${#IMAGES[@]} -eq 0 ]]; then
  IMAGES=(
    docker.io/library/fedora:latest
    docker.io/library/archlinux:latest
    docker.io/opensuse/tumbleweed:latest
    docker.io/library/debian:stable-slim
    docker.io/library/almalinux:9
    docker.io/library/ubuntu:22.04
  )
fi
CONTAINER="${CONTAINER:-}"
if [[ -z "${CONTAINER}" ]]; then
  if command -v docker >/dev/null 2>&1; then CONTAINER=docker; else CONTAINER=podman; fi
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
SERVER_PID=""
cleanup() {
  [[ -n "${SERVER_PID}" ]] && kill "${SERVER_PID}" 2>/dev/null || true
  rm -rf "${WORK}"
}
trap cleanup EXIT

cp "${APPIMAGE}" "${WORK}/app.AppImage"
chmod +x "${WORK}/app.AppImage"
cp "${BASH_SOURCE[0]}" "${WORK}/check.sh"

failed=()

echo "=== symlinks in the AppImage"
if ! (
  cd "${WORK}"
  APPIMAGE_EXTRACT_AND_RUN=1 ./app.AppImage --appimage-extract >/dev/null
  bad=0
  while IFS= read -r -d '' link; do
    target="$(readlink -f "${link}" || true)"
    if [[ -z "${target}" || "${target}" != "${WORK}/squashfs-root/"* || ! -e "${target}" ]]; then
      echo "FAIL: ${link#squashfs-root/} -> $(readlink "${link}")"
      bad=1
    fi
  done < <(find squashfs-root -type l -print0)
  rm -rf squashfs-root
  [[ ${bad} -eq 0 ]] && echo "ok: every symlink points inside the AppImage"
  exit "${bad}"
); then
  failed+=("symlinks")
fi

if [[ -n "${NNTPS_TESTSERVER:-}" ]]; then
  cp "${NNTPS_TESTSERVER}" "${WORK}/nntps-testserver"
else
  go build -C "${ROOT}/e2e" -o "${WORK}/nntps-testserver" ./cmd/nntps-testserver
fi
"${WORK}/nntps-testserver" -addr "127.0.0.1:${PORT}" -ca-out "${WORK}/ca.pem" >"${WORK}/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 100); do
  grep -q '^listening' "${WORK}/server.log" 2>/dev/null && break
  kill -0 "${SERVER_PID}" 2>/dev/null || { cat "${WORK}/server.log" >&2; exit 1; }
  sleep 0.1
done
grep -q '^listening' "${WORK}/server.log" || { echo "test server did not start" >&2; exit 1; }

for image in "${IMAGES[@]}"; do
  if ! "${CONTAINER}" run --rm --network host -e PORT="${PORT}" \
      -v "${WORK}:/work:ro,Z" "${image}" bash /work/check.sh --inside; then
    failed+=("${image}")
  fi
done
if [[ ${#failed[@]} -gt 0 ]]; then
  echo "FAILED on: ${failed[*]}" >&2
  exit 1
fi
echo "AppImage TLS check passed on ${#IMAGES[@]} distributions"
