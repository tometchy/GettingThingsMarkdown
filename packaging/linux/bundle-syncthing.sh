#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
arch="${1:-$(uname -m)}"
case "$arch" in
  x86_64|amd64) upstream=amd64; runtime=linux-x64 ;;
  aarch64|arm64) upstream=arm64; runtime=linux-arm64 ;;
  *) echo "Unsupported Linux architecture: $arch" >&2; exit 1 ;;
esac

version=2.1.7
name="syncthing-linux-${upstream}-v${version}"
url="https://github.com/syncthing/syncthing/releases/download/v${version}/${name}.tar.gz"
dest="src/Client/Desktop/native/$runtime"
mkdir -p "$dest"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl --fail --location --retry 3 --output "$tmp/syncthing.tar.gz" "$url"
# A verified checksum is mandatory. Fetch upstream release metadata if the
# checksums file layout differs; never silently skip verification.
curl --fail --location --retry 3 --output "$tmp/sha256sum.txt" \
  "https://github.com/syncthing/syncthing/releases/download/v${version}/sha256sum.txt"
( cd "$tmp"; grep -F " ${name}.tar.gz" sha256sum.txt | sha256sum -c - )
tar -xzf "$tmp/syncthing.tar.gz" -C "$tmp" "$name/syncthing"
install -m 0755 "$tmp/$name/syncthing" "$dest/syncthing"
echo "Bundled Syncthing $version for $runtime"
