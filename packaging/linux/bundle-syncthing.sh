#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
arch="${1:-$(uname -m)}"
case "$arch" in
  x86_64|amd64)
    upstream=amd64; runtime=linux-x64
    sha256=2edc416e7ed136b7c858ed5f3f02a3b8316589605e54c380228ceea7acbf0142 ;;
  aarch64|arm64)
    upstream=arm64; runtime=linux-arm64
    sha256=48e456690cd2b97e7d26b02b5ac35f62a3b36172267b144cfeaf02c993ab955c ;;
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
# SHA-256 digests pinned to GitHub release asset metadata for v2.1.7.
# Never accept an archive whose digest differs from this reviewed release.
printf '%s  %s\\n' "$sha256" "$tmp/syncthing.tar.gz" | sha256sum -c -
tar -xzf "$tmp/syncthing.tar.gz" -C "$tmp" "$name/syncthing"
install -m 0755 "$tmp/$name/syncthing" "$dest/syncthing"
echo "Bundled Syncthing $version for $runtime"
