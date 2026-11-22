#!/usr/bin/env bash
set -euo pipefail
api="${GTM_LOCAL_API:-http://127.0.0.1:5080}"
: "${GTM_E2E_ACCOUNT_ID:?Set GTM_E2E_ACCOUNT_ID from a registered test account}"
: "${GTM_E2E_JWT:?Set GTM_E2E_JWT from a registered test account}"
ns="gtm-${GTM_E2E_ACCOUNT_ID//-/}"
root="$(mktemp -d)"
name="gtm-e2e-$RANDOM"
port=22001
api_key="$(openssl rand -hex 24)"
password="$(openssl rand -base64 32)"
cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  if [ -n "${pf:-}" ]; then kill "$pf" 2>/dev/null || true; fi
  rm -rf "$root" 2>/dev/null || true # Container-owned Syncthing database in ephemeral CI workspace
}
trap cleanup EXIT

mkdir -p "$root/state" "$root/files/ToDo"
chmod 777 "$root/state" "$root/files" "$root/files/ToDo"

kubectl -n "$ns" port-forward svc/sync "$port:22000" >"$root/forward.log" 2>&1 &
pf=$!
docker run -d --name "$name" --network host -v "$root/state:/state" -v "$root/files:/files" \
  --entrypoint /bin/syncthing ghcr.io/syncthing/syncthing:2.1.7 \
  serve --home=/state --no-browser --gui-address=127.0.0.1:8384 \
  --gui-apikey="$api_key" >/dev/null

st() { curl -fsS --connect-timeout 3 -H "X-API-Key: $api_key" "$@"; }
for _ in $(seq 1 50); do
  if st http://127.0.0.1:8384/rest/system/status >"$root/status.json" 2>/dev/null; then break; fi
  sleep 1
done
[ -s "$root/status.json" ] || { docker logs "$name" | tail -50; exit 1; }
client_id="$(jq -r .myID "$root/status.json")"
echo "Client device ID created"
# Fail before pairing if shell serialization damaged the token or account ID.
if [[ ! "$GTM_E2E_ACCOUNT_ID" =~ ^[a-f0-9-]{36}$ ]] ||
   [ "$(awk -F. '{print NF}' <<< "$GTM_E2E_JWT")" != 3 ]; then
  echo "Invalid account ID/JWT from provisioning smoke test" >&2
  exit 1
fi

result="$(curl -fsS -X POST -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $GTM_E2E_JWT" \
  -d "$(jq -nc --arg id "$client_id" '{deviceId:$id,name:"e2e-client"}')" \
  "$api/api/devices")"
remote_id="$(jq -r '.serverDeviceId' <<< "$result")"
[ -n "$remote_id" ] && [ "$remote_id" != null ] || exit 1

device="$(st http://127.0.0.1:8384/rest/config/defaults/device)"
jq -c --arg id "$remote_id" \
  '.deviceID=$id | .name="encrypted-server" | .untrusted=true | .addresses=["tcp://127.0.0.1:22001"]' \
  <<< "$device" | st -X POST -H 'Content-Type: application/json' --data-binary @- \
    http://127.0.0.1:8384/rest/config/devices >/dev/null

folder="$(st http://127.0.0.1:8384/rest/config/defaults/folder)"
jq -c --arg own "$client_id" --arg remote "$remote_id" --arg pwd "$password" \
  '.id="notes" | .label="E2E notes" | .path="/files" | .type="sendreceive" |
   .devices=[{"deviceID":$own},{"deviceID":$remote,"encryptionPassword":$pwd}]' \
  <<< "$folder" | st -X POST -H 'Content-Type: application/json' --data-binary @- \
    http://127.0.0.1:8384/rest/config/folders >/dev/null

printf '%s\n' '---' 'status: doing' '---' '# Test' \
  'PRIVATE_E2E_SENTINEL_8c2f' > "$root/files/ToDo/test.md"

echo "Waiting for encrypted transfer and Git push..."
succeeded=0
for _ in $(seq 1 90); do
  if kubectl -n "$ns" exec sync-0 -- \
      sh -c 'find /data/files -type f | grep -v .stfolder | grep -q .' >/dev/null 2>&1 && \
     kubectl -n "$ns" exec sync-0 -- \
      env GIT_DIR=/data/git GIT_WORK_TREE=/data/files git rev-parse --verify HEAD >/dev/null 2>&1 && \
     kubectl -n "$ns" exec history-0 -- \
      git --git-dir=/var/lib/gitea/git/repositories/archive/history.git rev-parse --verify main >/dev/null 2>&1; then
    succeeded=1; break
  fi
  sleep 3
done
if [ "$succeeded" != 1 ]; then
  kubectl -n "$ns" logs sync-0 --tail=100
  kubectl -n "$ns" logs history-0 --tail=100
  echo "No end-to-end encrypted Sync -> Git -> Gitea transfer observed" >&2
  exit 1
fi

if kubectl -n "$ns" exec sync-0 -- \
    sh -c 'grep -R -q PRIVATE_E2E_SENTINEL_8c2f /data/files' ; then
  echo "CRITICAL: plaintext appeared in the untrusted server." >&2
  exit 1
fi
local_head="$(kubectl -n "$ns" exec sync-0 -- \
  env GIT_DIR=/data/git GIT_WORK_TREE=/data/files git rev-parse HEAD | tr -d '\r')"
remote_head="$(kubectl -n "$ns" exec history-0 -- \
  git --git-dir=/var/lib/gitea/git/repositories/archive/history.git rev-parse main | tr -d '\r')"
[ "$local_head" = "$remote_head" ] || {
  echo "Encrypted history divergence between A and B" >&2; exit 1
}
# Restore from the encrypted Git archive in B, not from A's local working copy.
# The native Syncthing decrypt implementation must recover the exact plaintext
# without any secret being sent to either SaaS container.
mkdir -p "$root/cipher" "$root/restored"
chmod 777 "$root/cipher" "$root/restored"
kubectl -n "$ns" exec history-0 -- \
  git --git-dir=/var/lib/gitea/git/repositories/archive/history.git archive main | \
  tar -C "$root/cipher" -xf -
docker run --rm --network none --user 1000:1000 \
  -e FOLDER_PASSWORD="$password" \
  -v "$root/cipher:/cipher:ro" -v "$root/restored:/plain" \
  --entrypoint /bin/syncthing ghcr.io/syncthing/syncthing:2.1.7 \
  decrypt --folder-id notes --to=/plain /cipher
if [ ! -f "$root/restored/ToDo/test.md" ] || \
   ! grep -q PRIVATE_E2E_SENTINEL_8c2f "$root/restored/ToDo/test.md"; then
  echo "Decryption of encrypted history from Gitea failed" >&2
  find "$root/restored" -maxdepth 5 -type f || true
  exit 1
fi
echo "PASS: Syncthing E2EE, encrypted Git commit, HTTPS push, Git history export and native offline decrypt"
