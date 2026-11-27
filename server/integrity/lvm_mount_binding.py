#!/usr/bin/env python3
"""Read-only pre-enrollment check of a real mounted local PV and its LV.

The independent inventory must pin the host, namespace, pod, node, PVC
and LV identities; an API label alone cannot establish volume ownership.
This tool does not create, enroll, freeze, mount or unmount anything.
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat

from recovery_crypto import RecoveryError, decode, read
from recovery_witness import require
from host_runtime_fence import UID
from local_lvm_fence import Lvm, configuration
from kube_client import Kube, ApiError
from namespace_fence import namespace_state, MARKER

NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
CLAIMS = {"sync": {"sync-data", "sync-live"}, "history": {"history-data"}}
FSTYPES = {"ext4", "xfs"}


def name(value):
    require(isinstance(value, str) and NAME.fullmatch(value), "invalid_kubernetes_name")
    return value


def bound_mounts(api, cfg, node_name, root):
    """Check API identities and exact local PV path (no arbitrary host paths)."""
    cfg = configuration(cfg)
    node_name = name(node_name)
    root = Path(root)
    require(root.is_absolute() and root != Path("/") and root == root.resolve(strict=True)
            and root.is_dir(), "invalid_mount_root")
    ns_path = "/api/v1/namespaces/" + cfg["tenant"]
    _, annotations = namespace_state(api.get(ns_path), cfg)
    require(MARKER not in annotations, "namespace_already_frozen")
    node = api.get("/api/v1/nodes/" + node_name)
    m = node["metadata"]
    require(m.get("name") == node_name and m.get("uid") == cfg["identity"]["nodeUid"]
            and not m.get("deletionTimestamp")
            and m.get("labels", {}).get("kubernetes.io/hostname"), "node_identity_mismatch")
    hostname = m["labels"]["kubernetes.io/hostname"]
    pod = api.get(ns_path + "/pods/" + cfg["role"] + "-0")
    pm, spec = pod["metadata"], pod["spec"]
    require(pm.get("uid") == cfg["identity"]["podUid"] and pm.get("namespace") == cfg["tenant"]
            and pm.get("name") == cfg["role"] + "-0" and not pm.get("deletionTimestamp")
            and spec.get("nodeName") == node_name and not spec.get("hostPID")
            and not spec.get("hostIPC") and not spec.get("hostNetwork"), "pod_identity_mismatch")
    require(all(not c.get("securityContext", {}).get("privileged")
                for c in spec.get("containers", []) + spec.get("initContainers", [])
                + spec.get("ephemeralContainers", [])), "privileged_writer_not_supported")
    require(not any("hostPath" in v for v in spec.get("volumes", [])), "host_path_writer_not_supported")
    names = [v["persistentVolumeClaim"]["claimName"] for v in spec.get("volumes", [])
             if "persistentVolumeClaim" in v]
    require(set(names) <= CLAIMS[cfg["role"]] and cfg["role"]+"-data" in names
            and len(names) == len(set(names)) == len(cfg["identity"]["pvcUids"]),
            "writer_claim_set_mismatch")
    mounts = {}
    pv_uids = set()
    for claim_name in names:
        pvc = api.get(ns_path + "/persistentvolumeclaims/" + name(claim_name))
        cm, cs = pvc["metadata"], pvc["spec"]
        uid = cm.get("uid")
        require(uid in cfg["identity"]["pvcUids"] and uid not in mounts
                and cm.get("name") == claim_name and cm.get("namespace") == cfg["tenant"]
                and not cm.get("deletionTimestamp") and pvc["status"]["phase"] == "Bound"
                and cs.get("volumeMode", "Filesystem") == "Filesystem"
                and cs.get("accessModes") == ["ReadWriteOnce"], "pvc_binding_mismatch")
        pv_name = name(cs["volumeName"])
        pv = api.get("/api/v1/persistentvolumes/" + pv_name)
        vm, vs = pv["metadata"], pv["spec"]
        ref = vs.get("claimRef", {})
        require(vm.get("name") == pv_name and isinstance(vm.get("uid"), str)
                and UID.fullmatch(vm["uid"]) and vm["uid"] not in pv_uids
                and not vm.get("deletionTimestamp") and pv["status"]["phase"] == "Bound"
                and ref.get("uid") == uid and ref.get("name") == claim_name
                and ref.get("namespace") == cfg["tenant"]
                and vs.get("persistentVolumeReclaimPolicy") == "Retain"
                and vs.get("volumeMode", "Filesystem") == "Filesystem"
                and vs.get("accessModes") == ["ReadWriteOnce"]
                and vs.get("storageClassName") == cs.get("storageClassName"),
                "pv_binding_mismatch")
        expected = root / uid
        require(vs.get("local", {}).get("path") == str(expected)
                and vs.get("nodeAffinity", {}).get("required", {}).get("nodeSelectorTerms") ==
                    [{"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In",
                                            "values": [hostname]}]}],
                "pv_path_or_node_affinity_mismatch")
        mounts[uid] = expected
        pv_uids.add(vm["uid"])
    require(set(mounts) == set(cfg["identity"]["pvcUids"]), "incomplete_pvc_binding")
    return mounts


def unescape(value):
    return re.sub(r"\\(040|011|012|134)", lambda m: chr(int(m.group(1), 8)), value)


def verify_mount(path, state, raw):
    """Reject parent filesystem fallback, subdirectory binds and stacked mounts."""
    path = Path(path)
    require(state["active"] and not state["readOnly"] and not state["activationSkip"]
            and state["device"], "lvm_not_active_writable")
    require(path == path.resolve(strict=True) and path.is_dir(), "invalid_pvc_mount_path")
    matches = []
    for line in raw.splitlines():
        left, right = line.split(" - ", 1)
        a, b = left.split(), right.split()
        require(len(a) >= 6 and len(b) == 3, "invalid_mountinfo")
        if unescape(a[4]) == str(path):
            matches.append((a, b))
    require(len(matches) == 1, "pvc_not_exactly_one_mount")
    a, b = matches[0]
    device = path.stat().st_dev
    require(a[2] == state["device"] == str(os.major(device))+":"+str(os.minor(device))
            and unescape(a[3]) == "/" and b[0] in FSTYPES
            and "rw" in a[5].split(",") and "rw" in b[2].split(","),
            "pvc_mount_device_or_mode_mismatch")
    return {"device": state["device"], "filesystem": b[0]}


def inspect(api, lvm, cfg, node_name, root, machine_id, proc="/proc/self/mountinfo"):
    cfg = configuration(cfg)
    require(machine_id == cfg["machineId"], "host_machine_identity_mismatch")
    paths = bound_mounts(api, cfg, node_name, root)
    raw = Path(proc).read_text()
    results = {}
    for pvc, path in paths.items():
        results[pvc] = verify_mount(path, lvm.inspect(cfg, pvc), raw)
    require(len({r["device"] for r in results.values()}) == len(results), "shared_mount_device")
    # Re-read API and the exact mount entries. This is an observation, not an
    # atomic enrollment transaction or protection against later mount changes.
    require(paths == bound_mounts(api, cfg, node_name, root), "binding_changed_during_inspection")
    latest = Path(proc).read_text()
    require(results == {p: verify_mount(path, lvm.inspect(cfg, p), latest)
                        for p, path in paths.items()}, "mount_changed_during_inspection")
    return {"schema": 1, "kind": "local_lvm_binding_observation", "tenant": cfg["tenant"],
            "role": cfg["role"], "identity": cfg["identity"], "namespaceUid": cfg["namespaceUid"],
            "verifiedMountCount": len(results), "physicalMountObserved": True,
            "enrollmentAuthorized": False, "activationAllowed": False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for field in ("inventory", "node-name", "mount-root", "kube-endpoint", "kube-ca", "kube-token"):
        p.add_argument("--"+field, required=True)
    a = p.parse_args()
    try:
        require(os.geteuid() == 0, "trusted_host_root_required")
        for path in (a.inventory, a.kube_ca, a.kube_token):
            s = Path(path).lstat()
            require(stat.S_ISREG(s.st_mode) and s.st_uid == 0 and not s.st_mode & 0o022,
                    "host_trust_file_not_root_owned")
        require(not Path(a.kube_token).stat().st_mode & 0o077, "insecure_host_credential")
        result = inspect(Kube(a.kube_endpoint, a.kube_ca, a.kube_token), Lvm(),
                         decode(read(a.inventory)), a.node_name, a.mount_root,
                         Path("/etc/machine-id").read_text().strip())
        print(json.dumps(result, separators=(",", ":")))
    except (RecoveryError, ApiError, OSError, ValueError, TypeError, KeyError, AttributeError,
            RuntimeError) as exc:
        print(json.dumps({"state": "blocked", "reason": str(exc) if isinstance(exc, RecoveryError)
                          else type(exc).__name__, "enrollmentAuthorized": False,
                          "activationAllowed": False}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
