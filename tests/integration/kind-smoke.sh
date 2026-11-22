#!/usr/bin/env bash
set -euo pipefail
command -v kubectl >/dev/null
command -v jq >/dev/null
command -v curl >/dev/null
api="${GTM_LOCAL_API:-http://127.0.0.1:5080}"
tag="$(date +%s)-$RANDOM"
register() {
  local email="$1"
  local response
  response="$(curl -fsS -H 'Content-Type: application/json' \
     -d "$(jq -nc --arg e "$email" \
       '{email:$e, password:"locally generated password of sufficient length", plan:"basic"}')" \
     "$api/api/auth/register")"
  echo "$response"
}

a="$(register "alpha-$tag@example.invalid")"
b="$(register "beta-$tag@example.invalid")"
a_id="$(jq -r '.account.id' <<< "$a")"
b_id="$(jq -r '.account.id' <<< "$b")"
[ "$a_id" != "$b_id" ]
ns_a="gtm-${a_id//-/}"; ns_b="gtm-${b_id//-/}"
echo "Created $ns_a and $ns_b"
for ns in "$ns_a" "$ns_b"; do
  kubectl get namespace "$ns" --request-timeout=10s || {
    echo "Tenant namespace creation is asynchronous, continuing to poll."
  }
done

for i in $(seq 1 45); do
  a_status="$(curl -fsS -H "Authorization: Bearer $(jq -r '.accessToken' <<< "$a")" \
    "$api/api/tenant" | jq -r '.status')"
  b_status="$(curl -fsS -H "Authorization: Bearer $(jq -r '.accessToken' <<< "$b")" \
    "$api/api/tenant" | jq -r '.status')"
  echo "Attempt $i/45: alpha=$a_status beta=$b_status"
  if [ "$a_status" = Ready ] && [ "$b_status" = Ready ]; then break; fi
  sleep 10
done
[ "$a_status" = Ready ] && [ "$b_status" = Ready ] || {
  kubectl -n gtm-system logs deploy/controlplane --tail=120 || true
  kubectl get pods -A -o wide
  exit 1
}

for ns in "$ns_a" "$ns_b"; do
  [ "$(kubectl -n "$ns" get statefulsets -o json | jq '.items|length')" = 2 ]
  [ "$(kubectl -n "$ns" get pvc -o json | jq '.items|length')" = 2 ]
  sync_node="$(kubectl -n "$ns" get pod sync-0 -o jsonpath='{.spec.nodeName}')"
  history_node="$(kubectl -n "$ns" get pod history-0 -o jsonpath='{.spec.nodeName}')"
  [ -n "$sync_node" ] && [ -n "$history_node" ]
  [ "$sync_node" != "$history_node" ] || {
    echo "Anti-affinity FAILED in $ns: both on $sync_node" >&2; exit 1
  }
  echo "$ns: Sync=$sync_node, History=$history_node"
done
echo "PASS: distinct tenants, two ready isolated StatefulSets, protected data volumes."
echo "NOTE: this smoke test does NOT yet validate file transfer, E2EE or history restore."
