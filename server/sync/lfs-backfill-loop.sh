#!/bin/sh
set -eu
[ "${GTM_GIT_LFS_ENABLED:-false}" = "true" ] || exit 0
until /usr/local/bin/lfs-backfill-once.sh; do
  echo 'WARN: historical encrypted LFS rehydration unavailable; will retry' >&2
  sleep "${GTM_LFS_BACKFILL_RETRY_SECONDS:-300}"
done
