#!/usr/bin/env bash
# Store independent recovery identities in separate cloud's Kubernetes Secret
# stores. Does NOT configure KMS at-rest encryption; an operator must first
# verify it. Default: dry-run, never copies one cloud's private key to another.
set -euo pipefail
if [[ $# -lt 4 || $# -gt 5 ]]; then
  echo "Usage: $0 <hetzner-context> <age-a-private-key-file> <ovh-context> <age-b-private-key-file> [--apply]" >&2
  exit 2
fi
context_a="$1"; key_a="$2"; context_b="$3"; key_b="$4"; mode="${5:---dry-run}"
[[ "$mode" == --apply || "$mode" == --dry-run ]] || exit 2
[[ "$context_a" != "$context_b" ]] || { echo "Cloud contexts must differ" >&2; exit 1; }
for bin in kubectl age-keygen; do command -v "$bin" >/dev/null || { echo "Missing $bin" >&2; exit 1; }; done
[[ -s "$key_a" && -s "$key_b" ]] || { echo "Both private age identity files required" >&2; exit 1; }
public_a="$(age-keygen -y "$key_a")"
public_b="$(age-keygen -y "$key_b")"
[[ "$public_a" == age1* && "$public_b" == age1* && "$public_a" != "$public_b" ]] || {
  echo "Invalid or identical age identities" >&2; exit 1;
}
uid_a="$(kubectl --context "$context_a" get ns kube-system -o jsonpath='{.metadata.uid}')"
uid_b="$(kubectl --context "$context_b" get ns kube-system -o jsonpath='{.metadata.uid}')"
[[ "$uid_a" != "$uid_b" ]] || {
  echo "Both contexts point to the SAME Kubernetes cluster, not two independent secret stores" >&2; exit 1;
}
for context in "$context_a" "$context_b"; do
  kubectl --context "$context" -n gtm-system get namespace gtm-system >/dev/null
done
echo 'Two distinct Kubernetes API clusters confirmed. Two age private keys differ.'
echo "Public recipient list for encrypted Git snapshots: $public_a,$public_b"
if [[ "$mode" == --dry-run ]]; then
  echo 'DRY RUN: no Kubernetes secrets changed. Configure and audit provider KMS / etcd encryption.'
  exit 0
fi
if [[ "${GTM_KMS_AT_REST_VERIFIED:-}" != yes ]]; then
  echo 'Set GTM_KMS_AT_REST_VERIFIED=yes ONLY after verifying KMS-backed etcd encryption on BOTH clusters.' >&2
  exit 1
fi
# kubectl --dry-run generates a YAML manifest in memory/pipeline, then uploads
# only to its corresponding provider; no private identity is copied across.
kubectl --context "$context_a" -n gtm-system create secret generic gtm-recovery-age \
  --from-file=identity.txt="$key_a" --dry-run=client -o yaml |
  kubectl --context "$context_a" -n gtm-system apply -f - >/dev/null
kubectl --context "$context_b" -n gtm-system create secret generic gtm-recovery-age \
  --from-file=identity.txt="$key_b" --dry-run=client -o yaml |
  kubectl --context "$context_b" -n gtm-system apply -f - >/dev/null
echo 'Two separate cloud-held private identities installed in isolated secret stores.'
echo 'CAUTION: Any K8s administrator able to read those Secrets can decrypt the tenant bootstrap packages.'
