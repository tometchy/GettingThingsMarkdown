#!/usr/bin/env bash
# Disposable three-member local cluster. No production endpoint or cloud.
set -euo pipefail
cd "$(dirname "$0")/../.."
network="gtmd-etcd-ci-$$"
names=("$network-a" "$network-b" "$network-c")
cleanup() {
  for n in "${names[@]}"; do docker logs "$n" --tail=15 2>/dev/null || true; done
  docker rm -f "${names[@]}" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker network create "$network" >/dev/null
cluster="${names[0]}=http://${names[0]}:2380,${names[1]}=http://${names[1]}:2380,${names[2]}=http://${names[2]}:2380"
args=()
for n in "${names[@]}"; do
  docker run -d --name "$n" --network "$network" -p 127.0.0.1::2379 \
    quay.io/coreos/etcd:v3.5.21 \
    /usr/local/bin/etcd --name "$n" --data-dir /etcd-data \
    --listen-client-urls http://0.0.0.0:2379 --advertise-client-urls "http://$n:2379" \
    --listen-peer-urls http://0.0.0.0:2380 --initial-advertise-peer-urls "http://$n:2380" \
    --initial-cluster "$cluster" --initial-cluster-state new --initial-cluster-token "$network" >/dev/null
  endpoint="$(docker port "$n" 2379/tcp | head -n1)"
  args+=(--endpoint "http://$endpoint")
done
python3 tests/integration/witness-etcd.py "${args[@]}" --stop "${names[1]}" --stop "${names[2]}"
