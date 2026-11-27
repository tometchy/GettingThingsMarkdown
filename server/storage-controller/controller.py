#!/usr/bin/env python3
"""GTMD storage controller. Only unbound local PV reservation may mutate K8s.

No bound PV, PVC, workload or data is modified. A repair proposal is not a
lease or a trusted recovery manifest, and cannot authorize live cutover.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import time

from kube_client import Kube, ApiError

DNS = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
DISK = re.compile(r"[0-9a-f]{24}\Z")
TENANT = re.compile(r"gtm-[0-9a-f]{32}\Z")
MAX_AGE = 300
REPORT_NAMESPACE = "gtm-storage-reports"
RESERVATION_NAMESPACE = "gtm-storage-quarantine"
PREFIX = "gtmd.io/"
CLAIMS = {"sync-data": "sync", "sync-live": "sync", "history-data": "history"}


def name(value):
    return isinstance(value, str) and DNS.fullmatch(value) and ".." not in value


def stamp(value, now):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= now - value <= MAX_AGE


def metadata(obj):
    return obj.get("metadata", {})


def ident(obj):
    return metadata(obj).get("name", "")


def valid_path(value):
    return isinstance(value, str) and value.startswith("/") and value != "/" and str(PurePosixPath(value)) == value and ".." not in PurePosixPath(value).parts


def inventory(raw):
    if not isinstance(raw, dict) or set(raw) != {"storageClasses", "nodes"}:
        raise ValueError("invalid_inventory")
    classes = raw["storageClasses"]
    if not isinstance(classes, list) or not classes or not all(name(x) for x in classes):
        raise ValueError("invalid_storage_classes")
    result = {}
    if not isinstance(raw["nodes"], list) or not raw["nodes"]:
        raise ValueError("empty_inventory")
    for node in raw["nodes"]:
        if set(node) != {"name", "uid", "hostname", "cloud", "disks"} or not all(
            name(node[k]) for k in ("name", "uid", "hostname", "cloud")):
            raise ValueError("invalid_node")
        if node["name"] in result or any(n["uid"] == node["uid"] or n["hostname"] == node["hostname"] for n in result.values()):
            raise ValueError("duplicate_node_identity")
        disks = {}
        for disk in node["disks"]:
            if set(disk) != {"id", "mount"} or not DISK.fullmatch(disk["id"]) or not valid_path(disk["mount"]):
                raise ValueError("invalid_disk_mapping")
            if disk["id"] in disks:
                raise ValueError("duplicate_disk")
            mount = PurePosixPath(disk["mount"])
            if any(mount == PurePosixPath(m) or mount in PurePosixPath(m).parents or PurePosixPath(m) in mount.parents for m in disks.values()):
                raise ValueError("overlapping_disk_mounts")
            disks[disk["id"]] = disk["mount"]
        if not disks:
            raise ValueError("empty_node_disks")
        result[node["name"]] = {**node, "disks": disks}
    return set(classes), result


def report_states(node, live_node, report, now):
    """Expected node inventory is independent of report; missing means UNKNOWN."""
    unknown = dict.fromkeys(node["disks"], "unknown")
    if not live_node or metadata(live_node).get("uid") != node["uid"]:
        return unknown
    labels = metadata(live_node).get("labels", {})
    if labels.get("kubernetes.io/hostname") != node["hostname"]:
        return unknown
    if not any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in live_node.get("status", {}).get("conditions", [])):
        return unknown
    if not report or metadata(report).get("annotations", {}).get(PREFIX + "node-uid") != node["uid"]:
        return unknown
    try:
        value = json.loads(report["data"]["report.json"])
        if (value["node"] != node["name"] or value["nodeUid"] != node["uid"] or
                value["inventoryOk"] is not True or not stamp(value["checkedUnix"], now)):
            return unknown
        disks = value["disks"]
        if not isinstance(disks, list) or len(disks) != len(node["disks"]):
            return unknown
        if {d["id"] for d in disks} != set(node["disks"]):
            return unknown
        return {d["id"]: d["state"] if d["state"] in ("healthy", "suspect", "failed", "unknown")
                and stamp(d["checkedUnix"], now) else "unknown" for d in disks}
    except (KeyError, TypeError, ValueError):
        return unknown


def mapping(pv, nodes):
    m, spec = metadata(pv), pv.get("spec", {})
    ann = m.get("annotations", {})
    node = nodes.get(ann.get(PREFIX + "node"))
    disk = ann.get(PREFIX + "disk-id")
    path = spec.get("local", {}).get("path")
    if not node or ann.get(PREFIX + "node-uid") != node["uid"] or disk not in node["disks"] or not valid_path(path):
        return None
    if PurePosixPath(node["disks"][disk]) not in PurePosixPath(path).parents:
        return None
    # Strict mapping contract: one term, one exact hostname, no OR bypasses.
    required = spec.get("nodeAffinity", {}).get("required", {})
    terms = required.get("nodeSelectorTerms", [])
    if len(terms) != 1 or terms[0].get("matchFields") or terms[0].get("matchExpressions") != [
            {"key": "kubernetes.io/hostname", "operator": "In", "values": [node["hostname"]]}]:
        return None
    return node["name"], disk


def bound_claim(pv, claims):
    ref = pv.get("spec", {}).get("claimRef", {})
    ns, claim = ref.get("namespace", ""), ref.get("name", "")
    if not TENANT.fullmatch(ns) or claim not in CLAIMS:
        return None
    pvc = claims.get((ns, claim))
    if not pvc or metadata(pvc).get("uid") != ref.get("uid") or not ref.get("uid"):
        return None
    if pvc.get("spec", {}).get("volumeName") != ident(pv) or pvc.get("status", {}).get("phase") != "Bound":
        return None
    return ns, claim, CLAIMS[claim]


def reserve_patch(pv):
    """CAS patch. Only Available Retain PV without claimRef is eligible."""
    m, spec = metadata(pv), pv.get("spec", {})
    if (pv.get("status", {}).get("phase") != "Available" or
            spec.get("claimRef") is not None or "claimRef" in spec or
            spec.get("persistentVolumeReclaimPolicy") != "Retain" or
            not m.get("uid") or not m.get("resourceVersion") or m.get("deletionTimestamp")):
        return None
    suffix = hashlib.sha256(m["uid"].encode()).hexdigest()[:32]
    return [
        {"op": "test", "path": "/metadata/uid", "value": m["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": m["resourceVersion"]},
        {"op": "test", "path": "/status/phase", "value": "Available"},
        {"op": "test", "path": "/spec/persistentVolumeReclaimPolicy", "value": "Retain"},
        {"op": "add", "path": "/spec/claimRef", "value": {
            "apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "namespace": RESERVATION_NAMESPACE, "name": "quarantine-" + suffix}}
    ]


def plan(config, live_nodes, reports, volumes, pvcs, now):
    classes, nodes = config
    live = {ident(n): n for n in live_nodes}
    report_by_name = {ident(r): r for r in reports}
    states = {n: report_states(node, live.get(n), report_by_name.get(n), now)
              for n, node in nodes.items()}
    claims = {(metadata(p).get("namespace"), ident(p)): p for p in pvcs}
    records, candidates = [], []
    for pv in volumes:
        spec, m = pv.get("spec", {}), metadata(pv)
        if spec.get("storageClassName") not in classes:
            continue
        where = mapping(pv, nodes)
        state = states[where[0]][where[1]] if where else "unknown"
        phase = pv.get("status", {}).get("phase", "Unknown")
        ref = spec.get("claimRef") or {}
        reserved = ref.get("namespace") == RESERVATION_NAMESPACE
        bound = bound_claim(pv, claims) if phase == "Bound" else None
        action = "none"
        if reserved:
            action = "reserved"
        elif state != "healthy":
            if reserve_patch(pv):
                action = "reserve_available"
            elif bound:
                action = "repair_required"
            else:
                action = "manual_review"
        elif where and reserve_patch(pv):
            candidates.append((pv, where))
        records.append({"pv": ident(pv), "uid": m.get("uid"), "where": where,
                        "state": state, "phase": phase, "bound": bound, "action": action,
                        "patch": reserve_patch(pv) if action == "reserve_available" else None})
    # Don't suggest a source merely because a disk reports healthy; an
    # independently authenticated Git manifest and fencing are still required.
    for record in records:
        if record["action"] != "repair_required":
            continue
        tenant, claim, role = record["bound"]
        source_role = "history" if role == "sync" else "sync"
        record["requiredSourceRole"] = source_role
        record["repairBlockedBy"] = ["trusted_manifest", "verified_source", "fencing", "retention"]
        record["candidatePVs"] = [ident(p) for p, w in candidates
            if record["where"] and w[0] != record["where"][0]
            and nodes[w[0]]["cloud"] == nodes[record["where"][0]]["cloud"]
            and p.get("spec", {}).get("storageClassName") ==
                next(v["spec"]["storageClassName"] for v in volumes if ident(v) == record["pv"])]
        # Capacity/access mode/quota constraints must be enforced at allocation;
        # names above are suggestions only, never binding decisions.
    return states, records


def reconcile(api, config, apply=False, now=None):
    now = time.time() if now is None else now
    nodes = list(api.list("/api/v1/nodes"))
    reports = list(api.list("/api/v1/namespaces/" + REPORT_NAMESPACE + "/configmaps"))
    pvs = list(api.list("/api/v1/persistentvolumes"))
    pvcs = list(api.list("/api/v1/persistentvolumeclaims"))
    states, records = plan(config, nodes, reports, pvs, pvcs, now)
    for record in records:
        if record["action"] == "reserve_available" and apply:
            try:
                api.patch("/api/v1/persistentvolumes/" + record["pv"], record["patch"])
                record["action"] = "reservation_requested"
            except ApiError as exc:
                # A concurrent binder or API rejection must never be retried
                # against stale data. Re-read entire inventory next cycle.
                record["action"] = "reservation_conflict" if exc.code in (409, 422) else "reservation_failed"
        record.pop("patch", None)
        # opaque IDs only; no mount paths, customer content or credentials.
        if record["action"] != "none":
            print(json.dumps({"event": "gtmd_storage_controller", **record},
                             separators=(",", ":")), flush=True)
    summary = {"event": "gtmd_storage_reconcile", "checkedUnix": now,
               "apply": apply, "disks": sum(len(v) for v in states.values()),
               "unhealthyDisks": sum(s != "healthy" for v in states.values() for s in v.values()),
               "boundRepairs": sum(x["action"] == "repair_required" for x in records),
               "reservations": sum(x["action"] in ("reserved", "reservation_requested") for x in records),
               "manualReview": sum(x["action"] in ("manual_review", "reservation_conflict", "reservation_failed") for x in records)}
    print(json.dumps(summary, separators=(",", ":")), flush=True)
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inventory", required=True)
    p.add_argument("--endpoint", default="https://kubernetes.default.svc")
    p.add_argument("--ca", default="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    p.add_argument("--token", default="/var/run/secrets/kubernetes.io/serviceaccount/token")
    p.add_argument("--reserve-available", action="store_true")
    p.add_argument("--once", action="store_true")
    args = p.parse_args()
    api = Kube(args.endpoint, args.ca, args.token)
    while True:
        try:
            reconcile(api, inventory(json.loads(Path(args.inventory).read_text())), args.reserve_available)
        except (OSError, ValueError, KeyError, TypeError, ApiError) as exc:
            print(json.dumps({"event": "gtmd_storage_reconcile", "state": "failed",
                              "reason": type(exc).__name__}), flush=True)
            if args.once:
                return 1
        else:
            if args.once:
                return 0
        time.sleep(60)


if __name__ == "__main__":
    raise SystemExit(main())
