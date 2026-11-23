#!/usr/bin/env bash
# Non-destructive, fail-closed preflight. This does NOT migrate provider PVCs,
# start a second writer or certify complete ransomware protection.
set -euo pipefail
if [[ $# -ne 2 || ! $1 =~ ^gtm-[0-9a-f]{32}$ || ! $2 =~ ^(sync|history)$ ]]; then
  echo "Usage: $0 gtm-<tenant-uuid-without-hyphens> sync|history" >&2
  exit 2
fi
ns="$1"
survivor="$2"
command -v kubectl >/dev/null || { echo "kubectl missing" >&2; exit 1; }
echo "Examining surviving role $survivor in namespace $ns (read only)"
if [[ "$survivor" == history ]]; then
  gitcmd=(git --git-dir=/var/lib/gitea/git/repositories/archive/history.git)
  pod=history-0
  kubectl -n "$ns" exec "$pod" -- "${gitcmd[@]}" fsck --full --no-reflogs --strict >/dev/null
  for ref in main gtm-recovery gtm-infra; do
    kubectl -n "$ns" exec "$pod" -- "${gitcmd[@]}" rev-parse --verify "$ref^{commit}" >/dev/null ||
      { echo "Missing $ref in History; abort failover" >&2; exit 1; }
  done
  echo "History Git exists and all three recovery refs were found."
else
  pod=sync-0
  kubectl -n "$ns" exec "$pod" -- git --git-dir=/data/git fsck --full --no-reflogs --strict >/dev/null
  kubectl -n "$ns" exec "$pod" -- git --git-dir=/data/git rev-parse --verify main >/dev/null
  for path in settings infra; do
    kubectl -n "$ns" exec "$pod" -- git -C "/data/recovery/$path" fsck --full --strict >/dev/null ||
      { echo "Recovery Git $path missing or corrupt" >&2; exit 1; }
  done
  echo "Sync local Git and encrypted recovery branches passed basic integrity checks."
fi
echo "Preflight does NOT prove that every snapshot exists, RPO=0 or that ciphertext is decryptable."
echo "DO NOT promote until the lost writer is fenced, the survivor's full Git is copied to"
echo "a freshly allocated provider-local PVC, and the age secret bundle is restored via"
echo "an authorized operator with a private key held in the surviving cloud."
