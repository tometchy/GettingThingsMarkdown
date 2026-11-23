#!/bin/sh
# Recreate a fully independent local LFS archive from the remote object
# store after old releases pruned A's cache. Never carry user plaintext.
# Runs in the existing Sync pod, not in another resident container.
set -eu
[ "${GTM_GIT_LFS_ENABLED:-false}" = "true" ] || exit 0
export GIT_DIR=/data/git GIT_WORK_TREE=/data/files
export GIT_ASKPASS=/usr/local/bin/askpass.sh
export GIT_TERMINAL_PROMPT=0
export GIT_SSL_CAINFO=/tls/tls.crt
if ! git ls-remote --exit-code --heads origin main >/dev/null 2>&1; then
  echo 'Remote Git history not yet initialized; LFS cannot be repaired' >&2
  exit 1
fi
git lfs fetch --all origin
# The exit status proves only that the Git LFS command completed.
# A separate complete-ref/OID inventory and audit is needed before failover.
date -u '+%Y-%m-%dT%H:%M:%SZ' > /data/git/gtm-lfs-backfill-last-ok.utc
echo 'Encrypted historical Git LFS backfill finished.'
