#!/usr/bin/env python3
"""One-shot repair allocation using the native static Kubernetes PV binder.

No PV mutation, dynamic provisioning, new database, daemon, pod, deletion,
or activation. Freeze first; reserve one isolated staging claim per incident.
The existing witness stores the immutable target UID after binding.
"""
import argparse
import json
from pathlib import Path, PurePosixPath
import time

from controller import inventory, mapping, report_states, metadata, ident, name, reserve_patch
from recovery_binding import bound, size
from kube_client import Kube, ApiError
from recovery_crypto import RecoveryError, digest, decode, read, verify
from recovery_witness import require, TENANT, ROLES, identifier
from writer_fence import identity
from witness_client import Client

NAMESPACE = "gtm-recovery"
SPARE = "gtmd.io/repair-spare"
PREFIX = "gtmd.io/"


def get_optional(api, path):
    try:
        return api.get(path)
    except ApiError as exc:
        if exc.code != 404:
            raise
        return None


def claim_path(tenant, claim):
    return "/api/v1/namespaces/" + tenant + "/persistentvolumeclaims/" + claim


def freeze_status(witness, public, tenant, incident, apply):
    def call(action, **fields):
        return witness.call({"tenant": tenant, "action": action, **fields})
    s = call("status")
    f = s.get("writerFence")
    require(f and f["phase"] in ("open", "freezing", "frozen"), "writer_fencing_not_enrolled")
    if f["phase"] == "open":
        if not apply:
            return None, s
        # Before touching storage or waiting for capacity.
        call("freeze-writers", incident=incident)
        s = call("status")
        f = s["writerFence"]
    envelope = call("resume-freeze")
    ch = verify(envelope, public)
    require(ch["schema"] == 1 and ch["kind"] == "writer_freeze"
            and ch["tenant"] == tenant and ch["incident"] == incident
            and ch["epoch"] == f["epoch"] and ch == f["freeze"]["challenge"]
            and f["phase"] in ("freezing", "frozen")
            and set(ch["writers"]) == set(ROLES)
            and all(identity(v) == f["writers"][role] for role, v in ch["writers"].items()),
            "active_freeze_identity_mismatch")
    return ch, s


def source_context(api, config, tenant, role, ch):
    classes, nodes = config
    enrolled = ch["writers"][role]
    other = ch["writers"]["history" if role == "sync" else "sync"]
    old_nodes = [n for n in nodes.values() if n["uid"] == enrolled["nodeUid"]]
    other_nodes = [n for n in nodes.values() if n["uid"] == other["nodeUid"]]
    require(len(old_nodes) == len(other_nodes) == 1 and old_nodes[0]["cloud"] != other_nodes[0]["cloud"],
            "replica_cloud_inventory_mismatch")
    old = old_nodes[0]
    claim = api.get(claim_path(tenant, role + "-data"))
    require(metadata(claim).get("name") == role + "-data"
            and metadata(claim).get("uid") in enrolled["pvcUids"], "original_claim_identity_mismatch")
    pv_name = claim["spec"]["volumeName"]
    require(name(pv_name), "invalid_original_pv")
    pv = api.get("/api/v1/persistentvolumes/" + pv_name)
    bound(claim, pv, tenant)
    where = mapping(pv, nodes)
    spec = pv["spec"]
    require(where and where[0] == old["name"] and spec["storageClassName"] in classes
            and spec.get("accessModes") == ["ReadWriteOnce"]
            and spec.get("volumeMode", "Filesystem") == "Filesystem",
            "original_volume_mapping_mismatch")
    capacity = size(spec["capacity"]["storage"])
    require(capacity > 0, "invalid_original_capacity")
    return old, where[1], capacity, metadata(claim)["uid"]


def compatible(api, config, repair_class, context, pv, volumes, now, health_cache=None):
    classes, nodes = config
    old, old_disk, capacity, _ = context
    m, spec = metadata(pv), pv["spec"]
    where = mapping(pv, nodes)
    if not (where and where[0] != old["name"] and where[1] != old_disk
            and nodes[where[0]]["cloud"] == old["cloud"]
            and spec.get("storageClassName") == repair_class
            and spec.get("persistentVolumeReclaimPolicy") == "Retain"
            and spec.get("accessModes") == ["ReadWriteOnce"]
            and spec.get("volumeMode", "Filesystem") == "Filesystem"
            and m.get("labels", {}).get(SPARE) == "true" and m.get("uid")
            and name(ident(pv)) and not m.get("deletionTimestamp")
            and size(spec["capacity"]["storage"]) >= capacity):
        return False
    # Reuse one health sample per candidate node during selection. Recheck
    # independently after creation/binding. No new polling service.
    node = nodes[where[0]]
    cache = {} if health_cache is None else health_cache
    if node["name"] not in cache:
        live = get_optional(api, "/api/v1/nodes/" + node["name"])
        report = get_optional(api, "/api/v1/namespaces/gtm-storage-reports/configmaps/" + node["name"])
        cache[node["name"]] = (report_states(node, live, report, now) if live
            and not metadata(live).get("deletionTimestamp") and not live.get("spec", {}).get("unschedulable") else {})
    if cache[node["name"]].get(where[1]) != "healthy":
        return False
    # Never use two local PV definitions of overlapping paths on one node.
    path = PurePosixPath(spec["local"]["path"])
    for v in volumes:
        if metadata(v).get("uid") == m["uid"]:
            continue
        loc = mapping(v, nodes)
        other_path = v.get("spec", {}).get("local", {}).get("path")
        if other_path and (loc is None or loc[0] == where[0]):
            p = PurePosixPath(other_path)
            if p == path or p in path.parents or path in p.parents:
                return False
    return True


def claim_spec(pv, tenant, role, ch, context, repair_class):
    _, _, capacity, old_uid = context
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {"name": "repair-" + digest(ch)[:32], "namespace": NAMESPACE,
                         "annotations": {PREFIX+"tenant": tenant, PREFIX+"role": role,
                                         PREFIX+"freeze-digest": digest(ch),
                                         PREFIX+"old-pvc-uid": old_uid,
                                         PREFIX+"target-pv-uid": metadata(pv)["uid"]}},
            "spec": {"storageClassName": repair_class, "volumeName": ident(pv),
                     "volumeMode": "Filesystem", "accessModes": ["ReadWriteOnce"],
                     "selector": {"matchLabels": {SPARE: "true"}},
                     "resources": {"requests": {"storage": str(capacity)}}}}


def validate_claim(actual, expected):
    m, s = metadata(actual), actual["spec"]
    em, es = expected["metadata"], expected["spec"]
    require(m.get("name") == em["name"] and m.get("namespace") == NAMESPACE
            and m.get("uid") and not m.get("deletionTimestamp")
            and all(m.get("annotations", {}).get(k) == v for k, v in em["annotations"].items())
            and s.get("storageClassName") == es["storageClassName"]
            and s.get("volumeName") == es["volumeName"]
            and s.get("volumeMode", "Filesystem") == "Filesystem"
            and s.get("accessModes") == es["accessModes"]
            and s.get("selector") == es["selector"]
            and not s.get("dataSource") and not s.get("dataSourceRef")
            and size(s["resources"]["requests"]["storage"]) == size(es["resources"]["requests"]["storage"]),
            "repair_claim_identity_mismatch")


def reconcile(api, witness, public, config, repair_class, tenant, incident, role, *,
              apply=False, now=time.time):
    require(isinstance(tenant, str) and TENANT.fullmatch(tenant) and identifier(incident)
            and role in ROLES, "invalid_repair_request")
    ch, status = freeze_status(witness, public, tenant, incident, apply)
    if ch is None:
        return {"state": "freeze_required", "activationAllowed": False}
    def result(state, **extra):
        return {"state": state, "tenant": tenant, "incident": incident, "role": role,
                "epoch": ch["epoch"], "activationAllowed": False, **extra}
    # Freeze even without an anchor. Never allocate without a verified anchor.
    if not status["generation"] or not status["anchorDigest"]:
        return result("waiting_for_anchor")
    require(name(repair_class) and repair_class in config[0], "invalid_repair_storage_class")
    sc = api.get("/apis/storage.k8s.io/v1/storageclasses/" + repair_class)
    require(sc["provisioner"] == "kubernetes.io/no-provisioner"
            and sc.get("volumeBindingMode") == "Immediate", "repair_class_must_be_static_immediate")
    context = source_context(api, config, tenant, role, ch)
    claim_name = "repair-" + digest(ch)[:32]
    path = claim_path(NAMESPACE, claim_name)
    actual = get_optional(api, path)
    plan = status["writerFence"]["freeze"].get("plan")
    if plan:
        require(actual is not None and plan["targetRole"] == role
                and plan["targetPvcUid"] == metadata(actual).get("uid"),
                "existing_repair_plan_conflict")
    volumes = list(api.list("/api/v1/persistentvolumes"))
    if actual is None:
        candidates = sorted((pv for pv in volumes if reserve_patch(pv)
                             and pv["spec"].get("storageClassName") == repair_class),
                            key=lambda v: (size(v["spec"]["capacity"]["storage"]), ident(v)))
        cache = {}
        pv = next((v for v in candidates if compatible(api, config, repair_class, context,
                   v, volumes, now(), cache)), None)
        if pv is None:
            return result("waiting_for_capacity")
        expected = claim_spec(pv, tenant, role, ch, context, repair_class)
        if not apply:
            return result("allocation_required", candidatePV=ident(pv))
        # One deterministic name is the reservation; volumeName prohibits
        # dynamic fallback or substitution. The native binder arbitrates PV
        # races. A lost POST reply is recovered by GET on the next invocation.
        try:
            api.request("POST", "/api/v1/namespaces/" + NAMESPACE + "/persistentvolumeclaims", expected)
        except ApiError as exc:
            if exc.code != 409:
                raise
        actual = api.get(path)
    pv_name = actual["spec"]["volumeName"]
    require(name(pv_name), "invalid_repair_pv")
    pv = api.get("/api/v1/persistentvolumes/" + pv_name)
    expected = claim_spec(pv, tenant, role, ch, context, repair_class)
    validate_claim(actual, expected)
    require(compatible(api, config, repair_class, context, pv, volumes, now()),
            "target_volume_no_longer_eligible")
    ref = pv["spec"].get("claimRef") or {}
    uid = metadata(actual)["uid"]
    if ref:
        require(ref.get("namespace") == NAMESPACE and ref.get("name") == claim_name
                and ref.get("uid") in (None, "", uid), "target_volume_claim_conflict")
    phase = actual.get("status", {}).get("phase", "Pending")
    if phase != "Bound" or pv["status"]["phase"] != "Bound":
        require(phase == "Pending"
                and pv["status"]["phase"] in ("Available", "Bound"), "repair_binding_failed")
        return result("waiting_for_binding", targetPvcUid=uid)
    bound(actual, pv, NAMESPACE)
    # Confirm witness incident/epoch again, before attaching immutable target.
    latest, s = freeze_status(witness, public, tenant, incident, False)
    require(latest == ch and s["generation"] == status["generation"]
            and s["anchorDigest"] == status["anchorDigest"], "freeze_changed_during_allocation")
    existing = s["writerFence"]["freeze"].get("plan")
    if existing:
        require(existing["targetRole"] == role and existing["targetPvcUid"] == uid,
                "existing_repair_plan_conflict")
    elif apply:
        witness.call({"tenant": tenant, "action": "plan-repair", "incident": incident,
                      "targetRole": role, "targetPvcUid": uid})
        _, s = freeze_status(witness, public, tenant, incident, False)
        require(s["writerFence"]["freeze"]["plan"]["targetPvcUid"] == uid
                and s["writerFence"]["freeze"]["plan"]["targetRole"] == role,
                "repair_plan_not_persisted")
    return result("allocated" if existing or apply else "binding_verified",
                  targetPvcUid=uid, targetNamespace=NAMESPACE,
                  targetPV=ident(pv), physicalMountVerified=False)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for field in ("inventory", "repair-class", "tenant", "incident", "role",
                  "kube-endpoint", "kube-ca", "kube-token", "witness-endpoint",
                  "witness-ca", "witness-cert", "witness-key", "witness-public"):
        p.add_argument("--"+field, required=True)
    p.add_argument("--apply-development-allocation", action="store_true")
    a = p.parse_args()
    try:
        result = reconcile(Kube(a.kube_endpoint, a.kube_ca, a.kube_token),
                           Client(a.witness_endpoint, a.witness_ca, a.witness_cert, a.witness_key),
                           a.witness_public, inventory(decode(read(a.inventory))), a.repair_class,
                           a.tenant, a.incident, a.role, apply=a.apply_development_allocation)
        print(json.dumps(result, separators=(",", ":")))
    except (RecoveryError, ApiError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        print(json.dumps({"state": "blocked", "reason": str(exc) if isinstance(exc, (RecoveryError, ValueError))
                          else type(exc).__name__, "activationAllowed": False}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
