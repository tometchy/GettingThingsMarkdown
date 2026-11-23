#!/usr/bin/env bash
# Decrypt an age-encrypted tenant secrets bundle from one surviving Git copy.
# By default only verifies its structure; mutation needs --apply, with fencing.
set -euo pipefail
if [[ $# -lt 3 || $# -gt 4 || ! $1 =~ ^gtm-[0-9a-f]{32}$ || ! $2 =~ ^(sync|history)$ ]]; then
  echo "Usage: $0 gtm-<32hex> sync|history /path/to/age-private-key [--apply]" >&2; exit 2
fi
ns="$1"; survivor="$2"; identity="$3"; mode="${4:---dry-run}"
[[ "$mode" == --apply || "$mode" == --dry-run ]] || exit 2
[[ -f "$identity" ]] || { echo "Age identity not found" >&2; exit 1; }
if [[ "$mode" == --apply && "${GTM_FENCE_ACKNOWLEDGED:-}" != yes ]]; then
  echo "Set GTM_FENCE_ACKNOWLEDGED=yes only AFTER independently fencing failed site" >&2
  exit 1
fi
for cmd in kubectl age jq; do command -v "$cmd" >/dev/null || { echo "$cmd not installed" >&2; exit 1; }; done
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
chmod 0700 "$tmp"
if [[ "$survivor" == history ]]; then
  kubectl -n "$ns" exec history-0 -- \
    git --git-dir=/var/lib/gitea/git/repositories/archive/history.git \
    show 'gtm-infra:tenant-secrets.json.age' > "$tmp/secrets.age"
else
  kubectl -n "$ns" exec sync-0 -- \
    cat /data/recovery/infra/tenant-secrets.json.age > "$tmp/secrets.age"
fi
age -d -i "$identity" -o "$tmp/secrets.json" "$tmp/secrets.age"
chmod 0600 "$tmp/secrets.json"
jq -e --arg ns "$ns" '
  .kind == "List" and
  (.items | length == 3) and
  ([.items[].metadata.name] | sort ==
   (["gtm-credentials","history-ca","history-tls"] | sort)) and
  all(.items[]; .kind == "Secret" and .metadata.namespace == $ns and
      (.data | type == "object"))
' "$tmp/secrets.json" >/dev/null || { echo "Invalid recovery secrets structure" >&2; exit 1; }
if [[ "$mode" == --apply ]]; then
  kubectl apply -f "$tmp/secrets.json" >/dev/null
  echo 'Recovery secrets restored. Validate client identity, key access and Git integrity.'
else
  echo 'PASS: encrypted secret package readable and valid. No resources changed.'
fi
