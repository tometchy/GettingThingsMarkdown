#!/bin/sh
set -eu
: "${GTM_WORM_BACKUP_ENABLED:?WORM backup must be explicitly enabled}"
while :; do
  if ! /usr/local/bin/backup-once.sh; then
    echo "CRITICAL: WORM metadata+LFS backup failed; operator action required" >&2
  fi
  sleep "${GTM_BACKUP_INTERVAL_SECONDS:-21600}" # every six hours by default
done
