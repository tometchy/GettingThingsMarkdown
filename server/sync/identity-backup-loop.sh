#!/bin/sh
set -eu
: "${GTM_RECOVERY_AGE_RECIPIENT:?}"
while :; do
  if ! /usr/local/bin/identity-backup-once.sh; then
    echo 'CRITICAL: encrypted Syncthing identity replication failed' >&2
  fi
  sleep "${GTM_RECOVERY_INTERVAL_SECONDS:-60}"
done
