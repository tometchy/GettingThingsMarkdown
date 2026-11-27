#!/usr/bin/env python3
"""Host publisher; patch ONLY the pre-created report ConfigMap for this node."""
import argparse
import json
import time
from pathlib import Path
from kube_client import Kube
from controller import name, stamp, DISK, REPORT_NAMESPACE, PREFIX


def report_value(path, node, uid, now):
    result = {"node": node, "nodeUid": uid, "checkedUnix": now,
              "inventoryOk": False, "disks": []}
    try:
        raw = json.loads(Path(path).read_text())
        if raw.get("inventoryOk") is not True or not stamp(raw["checkedUnix"], now):
            return result
        disks = [{"id": d["id"], "state": d["state"], "checkedUnix": d["checkedUnix"]}
                 for d in raw["disks"]]
        if not disks or len({d["id"] for d in disks}) != len(disks) or not all(
                DISK.fullmatch(d["id"]) and d["state"] in ("healthy", "suspect", "failed", "unknown")
                and stamp(d["checkedUnix"], now) for d in disks):
            return result
        return {**result, "checkedUnix": raw["checkedUnix"], "inventoryOk": True, "disks": disks}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return result


def publish(api, path, node, uid, now=None):
    if not name(node) or not name(uid):
        raise ValueError("invalid_node_identity")
    endpoint = "/api/v1/namespaces/" + REPORT_NAMESPACE + "/configmaps/" + node
    current = api.get(endpoint)
    m = current["metadata"]
    if m.get("annotations", {}).get(PREFIX + "node-uid") != uid:
        raise ValueError("report_identity_mismatch")
    value = report_value(path, node, uid, time.time() if now is None else now)
    api.patch(endpoint, [{"op": "test", "path": "/metadata/resourceVersion", "value": m["resourceVersion"]},
                         {"op": "add", "path": "/data", "value": {
                             "report.json": json.dumps(value, separators=(",", ":"))}}])
    return value["inventoryOk"]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("node", "uid", "endpoint", "ca", "token"):
        p.add_argument("--" + key, required=True)
    p.add_argument("--report", default="/var/lib/gtmd-disk-health/disk-health.json")
    args = p.parse_args()
    return 0 if publish(Kube(args.endpoint, args.ca, args.token), args.report,
                        args.node, args.uid) else 1


if __name__ == "__main__":
    raise SystemExit(main())
