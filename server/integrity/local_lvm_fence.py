#!/usr/bin/env python3
"""Experimental per-PVC LVM isolation on a trusted Linux host.

No forced/lazy unmount, no whole-disk fence, no activation or signed stop
proof. Requires a separately verified namespace admission barrier and
current witness freeze. Unsupported/shared storage remains blocked.
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

from recovery_crypto import RecoveryError, decode, digest, read
from recovery_witness import require, TENANT
from writer_fence import identity
from host_runtime_fence import UID, Cri, challenge, runtime_view, kernel_view
from witness_client import Client
from kube_client import Kube, ApiError
from namespace_fence import namespace_state, MARKER

LVM_UUID = re.compile(r"[A-Za-z0-9]{6}(?:-[A-Za-z0-9]{4}){5}-[A-Za-z0-9]{6}\Z")
MACHINE = re.compile(r"[a-f0-9]{32}\Z")
VG = re.compile(r"gtmd-[a-z0-9][a-z0-9-]{0,60}\Z")
FIELDS = ("vg_name,vg_uuid,lv_name,lv_uuid,lv_attr,lv_kernel_major,"
          "lv_kernel_minor,lv_tags,segtype,seg_count")


def configuration(raw):
    require(isinstance(raw, dict) and set(raw) == {"tenant", "role", "identity",
            "namespaceUid", "machineId", "volumes"}, "invalid_lvm_inventory")
    require(isinstance(raw["tenant"], str) and TENANT.fullmatch(raw["tenant"])
            and raw["role"] in ("sync", "history")
            and isinstance(raw["namespaceUid"], str) and UID.fullmatch(raw["namespaceUid"])
            and isinstance(raw["machineId"], str) and MACHINE.fullmatch(raw["machineId"]),
            "invalid_lvm_inventory")
    ids = identity(raw["identity"])
    require(all(UID.fullmatch(v) for v in [ids["podUid"], ids["nodeUid"], *ids["pvcUids"]]),
            "expected_kubernetes_uuid")
    volumes = raw["volumes"]
    require(isinstance(volumes, dict) and set(volumes) == set(ids["pvcUids"]),
            "incomplete_lvm_inventory")
    seen = set()
    for pvc, entry in volumes.items():
        require(isinstance(entry, dict) and set(entry) == {"vgName", "vgUuid", "lvUuid"}
                and isinstance(entry["vgName"], str) and VG.fullmatch(entry["vgName"])
                and all(isinstance(entry[k], str) and LVM_UUID.fullmatch(entry[k])
                        for k in ("vgUuid", "lvUuid")), "invalid_lvm_volume")
        require(entry["lvUuid"] not in seen, "shared_lvm_volume")
        seen.add(entry["lvUuid"])
    return raw


def selector(pvc, entry):
    return ("vg_uuid=" + entry["vgUuid"] + " && lv_uuid=" + entry["lvUuid"]
            + " && vg_name=" + entry["vgName"] + " && lv_name=pvc-" + pvc)


def dm_uuid(entry):
    return "LVM-" + (entry["vgUuid"] + entry["lvUuid"]).replace("-", "")


def mapped_devices(sys_block, entry):
    result = []
    for p in Path(sys_block).glob("dm-*"):
        # Fail closed if sysfs changes or is inaccessible during a sample.
        if (p / "dm/uuid").read_text().strip() == dm_uuid(entry):
            result.append({"dev": (p / "dev").read_text().strip(),
                           "ro": (p / "ro").read_text().strip(),
                           "holders": len(list((p / "holders").iterdir()))})
    return result


def volume_state(row, mapped, cfg, pvc):
    entry = cfg["volumes"][pvc]
    require(row["vg_name"] == entry["vgName"] and row["vg_uuid"] == entry["vgUuid"]
            and row["lv_name"] == "pvc-" + pvc and row["lv_uuid"] == entry["lvUuid"],
            "lvm_identity_mismatch")
    tags = set(row["lv_tags"].split(","))
    require({"gtmd.tenant." + cfg["tenant"], "gtmd.role." + cfg["role"],
             "gtmd.pvc." + pvc} <= tags
            and {t for t in tags if t.startswith("gtmd.tenant.")} == {"gtmd.tenant." + cfg["tenant"]}
            and {t for t in tags if t.startswith("gtmd.pvc.")} == {"gtmd.pvc." + pvc}
            and {t for t in tags if t.startswith("gtmd.role.")} == {"gtmd.role." + cfg["role"]},
            "lvm_owner_tags_mismatch")
    attr = row["lv_attr"]
    # Reject snapshots, thin/RAID/cache, partial, suspended, open and
    # unfamiliar modes. One linear segment on one underlying device only.
    require(row["segtype"] == "linear" and row["seg_count"] == "1"
            and len(attr) == 10 and attr[0] == "-" and attr[1] in ("w", "r")
            and attr[4] in ("a", "-") and attr[5] in ("o", "-")
            and attr[6] == "-" and attr[7] == "-" and attr[8] == "-"
            and attr[9] in ("-", "k"), "unsupported_lvm_state")
    active = attr[4] == "a"
    if active:
        device = row["lv_kernel_major"] + ":" + row["lv_kernel_minor"]
        require(re.fullmatch(r"[1-9][0-9]*:[0-9]+", device)
                and len(mapped) == 1 and mapped[0]["dev"] == device
                and mapped[0]["holders"] == 0 and mapped[0]["ro"] in ("0", "1"),
                "lvm_kernel_mapping_mismatch")
        require(attr[1] != "r" or mapped[0]["ro"] == "1", "lvm_kernel_not_read_only")
    else:
        require(not mapped and row["lv_kernel_major"] == "-1"
                and row["lv_kernel_minor"] == "-1" and attr[5] == "-",
                "inactive_lvm_mapping_inconsistent")
        device = None
    return {"active": active, "open": attr[5] == "o", "readOnly": attr[1] == "r",
            "activationSkip": attr[9] == "k", "device": device}


class Lvm:
    def __init__(self, sys_block="/sys/block"):
        self.sys_block = Path(sys_block)
        require(self.sys_block.is_dir(), "missing_sysfs")

    def run(self, *args):
        result = subprocess.run(["/usr/sbin/lvm", *args], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
                                check=True, timeout=40)
        require(len(result.stdout) <= 4 * 1024 * 1024, "lvm_response_too_large")
        return result.stdout

    def inspect(self, cfg, pvc):
        entry = cfg["volumes"][pvc]
        raw = decode(self.run("lvs", "--reportformat", "json", "--segments",
                              "-o", FIELDS, "--select", selector(pvc, entry)))
        reports = raw["report"]
        require(isinstance(reports, list) and len(reports) == 1, "unexpected_lvm_report")
        rows = reports[0]["seg"]
        require(isinstance(rows, list) and len(rows) == 1, "lvm_volume_not_unique")
        return volume_state(rows[0], mapped_devices(self.sys_block, entry), cfg, pvc)

    def restrict(self, cfg, pvc):
        self.run("lvchange", "--permission", "r", "--setactivationskip", "y",
                 "--select", selector(pvc, cfg["volumes"][pvc]))

    def deactivate(self, cfg, pvc):
        self.run("lvchange", "--activate", "n", "--select", selector(pvc, cfg["volumes"][pvc]))


def isolated(value):
    return not value["active"] and not value["open"] and value["readOnly"] and value["activationSkip"]


def current_freeze(witness, kube, public, cfg, now):
    ch = challenge(witness.call({"tenant": cfg["tenant"], "action": "resume-freeze"}),
                   public, cfg, int(now()))
    _, annotations = namespace_state(kube.get("/api/v1/namespaces/" + cfg["tenant"]), cfg)
    require(annotations.get(MARKER) == digest(ch), "current_namespace_freeze_marker_missing")
    return ch


def preflight(lvm, cri, cfg, kernel):
    states = {pvc: lvm.inspect(cfg, pvc) for pvc in cfg["identity"]["pvcUids"]}
    devices = {pvc: s["device"] for pvc, s in states.items() if s["device"] is not None}
    require(len(set(devices.values())) == len(devices), "shared_kernel_device")
    first = runtime_view(*cri.list(), cfg)
    k = kernel("/proc", {**cfg, "devices": devices})
    last = runtime_view(*cri.list(), cfg)
    require(first == last and not last["replacementCount"] and not last["activeSandboxCount"]
            and not last["nonExitedContainerCount"] and not k["writerProcessCount"],
            "writer_activity_or_runtime_churn")
    # Even a read-only mount or open block handle prevents deactivation.
    require(not k["matchingMountCount"] and not any(v["open"] for v in states.values()),
            "lvm_volume_still_mounted_or_open")
    return states


def reconcile(lvm, cri, kube, witness, public, cfg, machine_id, *, apply=False,
              now=time.time, kernel=kernel_view):
    cfg = configuration(cfg)
    require(machine_id == cfg["machineId"], "host_machine_identity_mismatch")
    ch = current_freeze(witness, kube, public, cfg, now)
    def guard():
        require(digest(current_freeze(witness, kube, public, cfg, now)) == digest(ch),
                "freeze_changed_during_lvm_isolation")
        require(int(now()) >= ch["notBeforeUnix"], "prior_permits_not_expired")
        return preflight(lvm, cri, cfg, kernel)
    states = preflight(lvm, cri, cfg, kernel)
    if apply:
        guard()
        for pvc in cfg["identity"]["pvcUids"]:
            state = guard()[pvc]
            if not state["readOnly"] or not state["activationSkip"]:
                lvm.restrict(cfg, pvc)
                state = guard()[pvc]
                require(state["readOnly"] and state["activationSkip"], "lvm_restriction_not_persisted")
            if state["active"]:
                lvm.deactivate(cfg, pvc)
            require(isolated(guard()[pvc]), "lvm_isolation_not_persisted")
        states = guard()
        require(all(isolated(s) for s in states.values()), "incomplete_lvm_isolation")
    all_isolated = all(isolated(s) for s in states.values())
    return {"schema": 1, "kind": "local_lvm_isolation_observation", "tenant": cfg["tenant"],
            "role": cfg["role"], "identity": cfg["identity"], "epoch": ch["epoch"],
            "challengeDigest": digest(ch), "observedUnix": int(now()),
            "isolatedVolumeCount": sum(isolated(s) for s in states.values()),
            "expectedVolumeCount": len(states), "state": "isolated" if all_isolated else "isolation_required",
            "localStorageIsolationObserved": all_isolated, "stopEvidenceEligible": False,
            "activationAllowed": False}


@contextlib.contextmanager
def host_lock():
    fd = os.open("/run/lock/gtmd-lvm-fence.lock",
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        s = os.fstat(fd)
        require(stat.S_ISREG(s.st_mode) and s.st_uid == 0 and not s.st_mode & 0o077,
                "insecure_host_lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for field in ("inventory", "witness-endpoint", "witness-ca", "witness-cert",
                  "witness-key", "witness-public", "kube-endpoint", "kube-ca",
                  "kube-token", "cri-socket"):
        p.add_argument("--" + field, required=True)
    p.add_argument("--apply-development-isolation", action="store_true")
    a = p.parse_args()
    try:
        require(os.geteuid() == 0, "trusted_host_root_required")
        for path in (a.inventory, a.witness_public, a.witness_ca, a.witness_key,
                     a.witness_cert, a.kube_ca, a.kube_token):
            s = Path(path).lstat()
            require(stat.S_ISREG(s.st_mode) and s.st_uid == 0 and not s.st_mode & 0o022,
                    "host_trust_file_not_root_owned")
        for path in (a.witness_key, a.kube_token):
            require(not Path(path).stat().st_mode & 0o077, "insecure_host_credential")
        with host_lock():
            result = reconcile(Lvm(), Cri(a.cri_socket), Kube(a.kube_endpoint, a.kube_ca, a.kube_token),
                               Client(a.witness_endpoint, a.witness_ca, a.witness_cert, a.witness_key),
                               a.witness_public, decode(read(a.inventory)),
                               Path("/etc/machine-id").read_text().strip(),
                               apply=a.apply_development_isolation)
        print(json.dumps(result, separators=(",", ":")))
    except (RecoveryError, ApiError, OSError, ValueError, KeyError, TypeError,
            AttributeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"state": "blocked", "reason": str(exc) if isinstance(exc, RecoveryError)
                          else type(exc).__name__, "stopEvidenceEligible": False, "activationAllowed": False}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
