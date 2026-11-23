#!/bin/sh
# No plaintext server identity is pushed to the second cloud.
# Runs inside the existing Syncthing+Git container, never as an extra pod.
set -eu
# This script runs as a subprocess of the Sync entrypoint, whose GIT_DIR and
# GIT_WORK_TREE refer to customer ciphertext. NEVER commit recovery secrets
# to that Git tree; this must be an independent local recovery branch.
unset GIT_DIR GIT_WORK_TREE
: "${GTM_RECOVERY_AGE_RECIPIENT:?Missing public recipient}"
: "${GTM_GIT_URL:?Missing recovery Git remote}"
# Accept a comma-separated set of independently controlled recipients. In
# production use one recipient per cloud plus an offline recovery recipient.
# The ciphertext stays in Git; none of the corresponding private identities
# is ever injected into a long-lived tenant workload.
recipients="$GTM_RECOVERY_AGE_RECIPIENT"
oldIFS="$IFS"
IFS=,
set --
for recipient in $recipients; do
  case "$recipient" in age1*) ;; *) echo "Invalid age recipient" >&2; exit 1 ;; esac
  set -- "$@" -r "$recipient"
done
IFS="$oldIFS"
[ "$#" -ge 2 ] || { echo "Two recovery recipients required (one per cloud)." >&2; exit 1; }
home=/data/syncthing
for file in config.xml cert.pem key.pem; do
  [ -s "$home/$file" ] || { echo "Identity $file missing; cannot back up" >&2; exit 1; }
done
# Use a separate Git directory/branch, not the encrypted customer data tree.
# The age decryption key does NOT exist in either SaaS tenant container.
repo=/data/recovery/settings
mkdir -p "$repo"
if [ ! -d "$repo/.git" ]; then
  git -C "$repo" init -q -b gtm-recovery
fi
git -C "$repo" config user.name 'GettingThingsMarkdown recovery'
git -C "$repo" config user.email 'recovery@localhost.invalid'
if git -C "$repo" remote get-url origin >/dev/null 2>&1; then
  git -C "$repo" remote set-url origin "$GTM_GIT_URL"
else
  git -C "$repo" remote add origin "$GTM_GIT_URL"
fi
fingerprint="$(cd "$home" && sha256sum config.xml cert.pem key.pem | sha256sum | awk '{print $1}')"
previous="$(cat "$repo/fingerprint.sha256" 2>/dev/null || true)"
if [ "$fingerprint" != "$previous" ] || [ ! -s "$repo/identity.tar.gz.age" ]; then
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT HUP INT TERM
  chmod 0700 "$tmp"
  tar -czf "$tmp/identity.tar.gz" -C "$home" config.xml cert.pem key.pem
  tar -tzf "$tmp/identity.tar.gz" >/dev/null
  age "$@" -o "$tmp/identity.tar.gz.age" "$tmp/identity.tar.gz"
  mv "$tmp/identity.tar.gz.age" "$repo/identity.tar.gz.age"
  printf '%s\n' "$fingerprint" > "$repo/fingerprint.sha256"
  git -C "$repo" add identity.tar.gz.age fingerprint.sha256
  git -C "$repo" commit -q -m "Encrypted server identity snapshot $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
fi
# Always retry a previous failed push, even when the source config is unchanged.
export GIT_ASKPASS=/usr/local/bin/askpass.sh
export GIT_TERMINAL_PROMPT=0
export GIT_SSL_CAINFO=/tls/tls.crt
git -C "$repo" push -q origin HEAD:refs/heads/gtm-recovery
echo 'Encrypted Syncthing identity replicated to History Git.'
