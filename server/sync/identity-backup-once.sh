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
seen=","
for recipient in $recipients; do
  case "$recipient" in age1*) ;; *) echo "Invalid age recipient" >&2; exit 1 ;; esac
  case "$seen" in *,"$recipient",*) echo "Duplicate age recipient" >&2; exit 1 ;; esac
  seen="$seen$recipient,"
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
# Rotating recipients must re-encrypt the same Syncthing identity, even if
# the config/certificate/key bytes themselves did not change.
mailhome=/data/mail-syncthing
fingerprint="$( (cd "$home" && sha256sum config.xml cert.pem key.pem; if [ -s "$mailhome/config.xml" ]; then (cd "$mailhome" && sha256sum config.xml cert.pem key.pem); fi; printf '%s\\n' "$recipients") | sha256sum | awk '{print $1}')"
previous="$(cat "$repo/fingerprint.sha256" 2>/dev/null || true)"
if [ "$fingerprint" != "$previous" ] || [ ! -s "$repo/identity.tar.gz.age" ]; then
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT HUP INT TERM
  chmod 0700 "$tmp"
  mkdir -p "$tmp/stage"
  cp "$home"/config.xml "$home"/cert.pem "$home"/key.pem "$tmp/stage/"
  if [ -s "$mailhome/config.xml" ]; then
    mkdir -p "$tmp/stage/mail-syncthing"
    cp "$mailhome"/config.xml "$mailhome"/cert.pem "$mailhome"/key.pem "$tmp/stage/mail-syncthing/"
    tar -czf "$tmp/identity.tar.gz" -C "$tmp/stage" config.xml cert.pem key.pem \
      mail-syncthing/config.xml mail-syncthing/cert.pem mail-syncthing/key.pem
  else
    tar -czf "$tmp/identity.tar.gz" -C "$tmp/stage" config.xml cert.pem key.pem
  fi
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
