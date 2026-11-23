#!/usr/bin/env bash
# Run on a TRUSTED operator machine. Plaintext Kubernetes Secret values are
# streamed through age, never written to disk or into either tenant container.
# Store the resulting age ciphertext in the SAME Git repository as tenant data,
# on a separate branch. Use --context to target the current authoritative API.
set -euo pipefail
if [[ $# -ne 1 || ! $1 =~ ^gtm-[0-9a-f]{32}$ ]]; then
  echo "Usage: GTM_RECOVERY_AGE_RECIPIENTS=age1...,age1... $0 gtm-<32hex>" >&2
  exit 2
fi
ns="$1"
for bin in kubectl jq age; do command -v "$bin" >/dev/null || { echo "$bin missing" >&2; exit 1; }; done
: "${GTM_RECOVERY_AGE_RECIPIENTS:?Comma-separated public age recipients required}"
IFS=, read -r -a keys <<< "$GTM_RECOVERY_AGE_RECIPIENTS"
if (( ${#keys[@]} < 2 )); then echo "At least two provider-held recovery recipients required" >&2; exit 1; fi
declare -A distinct=()
args=()
for key in "${keys[@]}"; do
  [[ "$key" =~ ^age1[a-z0-9]{20,100}$ ]] || { echo "Invalid age recipient" >&2; exit 1; }
  if [[ -n "${distinct[$key]+yes}" ]]; then echo "Duplicate age key" >&2; exit 1; fi
  distinct["$key"]=1
  args+=(-r "$key")
done
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
chmod 0700 "$tmp"
# These are the authoritative K8s secrets needed to rebuild both tenant pods.
# Gitea SQLite is reconstructed by bootstrap; account/Stripe DB is a separate
# shared control-plane recovery dependency and must also be backed up.
kubectl -n "$ns" get secrets gtm-credentials history-tls history-ca -o json |
  jq --arg namespace "$ns" -c '{
      apiVersion:"v1",kind:"List",
      items:[.items[] | {apiVersion:"v1",kind:"Secret",
        metadata:{name:.metadata.name,namespace:$namespace},
        type:.type,data:.data}]
    }' | age "${args[@]}" -o "$tmp/tenant-secrets.json.age"
chmod 0600 "$tmp/tenant-secrets.json.age"

# Only encrypted data crosses into Sync; the operator's plaintext secret
# export goes directly to age via the pipe above.
kubectl -n "$ns" exec sync-0 -- mkdir -p /data/recovery/infra
kubectl -n "$ns" cp "$tmp/tenant-secrets.json.age" "sync-0:/data/recovery/infra/tenant-secrets.json.age"
kubectl -n "$ns" exec sync-0 -- sh -ec '
  set -eu
  unset GIT_DIR GIT_WORK_TREE
  repo=/data/recovery/infra
  cd "$repo"
  [ -d .git ] || git init -q -b gtm-infra
  git config user.name "GettingThingsMarkdown recovery"
  git config user.email "recovery@localhost.invalid"
  if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "$GTM_GIT_URL"
  else
    git remote add origin "$GTM_GIT_URL"
  fi
  git add tenant-secrets.json.age
  if ! git diff --cached --quiet; then
    git commit -q -m "Encrypted tenant bootstrap secrets $(date -u +%FT%TZ)"
  fi
  GIT_ASKPASS=/usr/local/bin/askpass.sh GIT_TERMINAL_PROMPT=0 \
    GIT_SSL_CAINFO=/tls/tls.crt \
    git push origin HEAD:refs/heads/gtm-infra
'
echo "Encrypted Kubernetes tenant credentials now retained in Sync and Gitea Git."
echo "WARNING: Store age decryption private keys securely in BOTH independent clouds; test both."
