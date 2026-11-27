#!/usr/bin/env python3
"""Project an active signed witness freeze into one Kubernetes tenant namespace.

This is a one-way admission barrier, not a physical fence or stop proof.
Credentials are scoped to GET/PATCH one independently provisioned namespace.
Run outside tenant workloads; never grant the actor policy management rights.
"""
import argparse
import json
import re
import time

from recovery_crypto import RecoveryError, decode, digest, read
from recovery_witness import require, TENANT
from host_runtime_fence import challenge, UID
from witness_client import Client
from kube_client import Kube, ApiError

MARKER = "gtmd.io/freeze-digest"
POINTER = "/metadata/annotations/gtmd.io~1freeze-digest"


def inventory(raw):
    require(isinstance(raw, dict) and set(raw) == {"tenant", "namespaceUid", "role", "identity"},
            "invalid_namespace_fence_inventory")
    require(isinstance(raw["tenant"], str) and TENANT.fullmatch(raw["tenant"])
            and isinstance(raw["namespaceUid"], str) and UID.fullmatch(raw["namespaceUid"])
            and raw["role"] in ("sync", "history"), "invalid_namespace_fence_inventory")
    from writer_fence import identity
    ids = identity(raw["identity"])
    require(all(UID.fullmatch(v) for v in [ids["podUid"], ids["nodeUid"], *ids["pvcUids"]]),
            "invalid_namespace_fence_identity")
    return raw


def actor_manifest(tenant, system_namespace="gtm-system"):
    """Generate scoped identities for trusted offline provisioning, never apply."""
    require(isinstance(tenant, str) and TENANT.fullmatch(tenant)
            and isinstance(system_namespace, str)
            and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", system_namespace),
            "invalid_fence_actor_scope")
    name = tenant + "-fence"
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": name, "namespace": system_namespace},
         "automountServiceAccountToken": False},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
         "metadata": {"name": name},
         "rules": [{"apiGroups": [""], "resources": ["namespaces"],
                    "resourceNames": [tenant], "verbs": ["get", "patch"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
         "metadata": {"name": name},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": name},
         "subjects": [{"kind": "ServiceAccount", "name": name, "namespace": system_namespace}]}
    ]}


def namespace_state(ns, cfg):
    m = ns["metadata"]
    require(m.get("name") == cfg["tenant"] and m.get("uid") == cfg["namespaceUid"]
            and not m.get("deletionTimestamp") and ns.get("status", {}).get("phase") == "Active"
            and isinstance(m.get("resourceVersion"), str) and bool(m["resourceVersion"]),
            "namespace_binding_not_verified")
    annotations = m.get("annotations", {})
    require(isinstance(annotations, dict), "invalid_namespace_annotations")
    return m, annotations


def patch_marker(m, value):
    operations = [
        {"op": "test", "path": "/metadata/uid", "value": m["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": m["resourceVersion"]},
    ]
    if "annotations" not in m:
        operations.append({"op": "add", "path": "/metadata/annotations", "value": {MARKER: value}})
    else:
        operations.append({"op": "add", "path": POINTER, "value": value})
    return operations


def confirm_marker_policy(api, path, m):
    # Deliberately invalid marker: the API must reject this exact admission
    # policy, not merely a missing RBAC permission or arbitrary 4xx response.
    try:
        api.patch(path + "?dryRun=All", patch_marker(m, "invalid"))
    except ApiError as exc:
        require(exc.code in (403, 422) and exc.category == "freeze_marker_admission_denied",
                "freeze_marker_policy_not_verified")
    else:
        raise RecoveryError("freeze_marker_policy_not_enforced")


def reconcile(api, witness, public, cfg, *, apply=False, now=time.time):
    cfg = inventory(cfg)
    # Read the active challenge online every reconciliation. No user-supplied
    # or cached challenge can authorize a namespace write.
    envelope = witness.call({"tenant": cfg["tenant"], "action": "resume-freeze"})
    ch = challenge(envelope, public, cfg, int(now()))
    token = digest(ch)
    path = "/api/v1/namespaces/" + cfg["tenant"]
    m, annotations = namespace_state(api.get(path), cfg)
    require(MARKER not in annotations or annotations[MARKER] == token,
            "different_namespace_freeze_marker")
    if apply:
        confirm_marker_policy(api, path, m)
        if MARKER not in annotations:
            # CAS on UID and resourceVersion; never blindly retry a conflict
            # or ambiguous network outcome. Reconcile afresh instead.
            api.patch(path, patch_marker(m, token))
        m, annotations = namespace_state(api.get(path), cfg)
        require(annotations.get(MARKER) == token, "freeze_marker_not_persisted")
    return {"tenant": cfg["tenant"], "role": cfg["role"], "epoch": ch["epoch"],
            "challengeDigest": token, "namespaceUid": cfg["namespaceUid"],
            "state": "marker_present" if annotations.get(MARKER) == token else "marker_required",
            "markerPolicyProbed": apply,
            "workloadAdmissionVerified": False, "stopEvidenceEligible": False,
            "activationAllowed": False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for field in ("inventory", "witness-endpoint", "witness-ca", "witness-cert",
                  "witness-key", "witness-public", "kube-endpoint", "kube-ca", "kube-token"):
        p.add_argument("--" + field, required=True)
    p.add_argument("--apply-development-freeze", action="store_true",
                   help="Irreversible namespace barrier; only for disposable development tenants.")
    a = p.parse_args()
    try:
        result = reconcile(Kube(a.kube_endpoint, a.kube_ca, a.kube_token),
                           Client(a.witness_endpoint, a.witness_ca, a.witness_cert, a.witness_key),
                           a.witness_public, decode(read(a.inventory)),
                           apply=a.apply_development_freeze)
        print(json.dumps(result, separators=(",", ":")))
    except (RecoveryError, ApiError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(json.dumps({"state": "blocked",
                         "reason": str(exc) if isinstance(exc, RecoveryError) else type(exc).__name__,
                         "activationAllowed": False, "stopEvidenceEligible": False}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
