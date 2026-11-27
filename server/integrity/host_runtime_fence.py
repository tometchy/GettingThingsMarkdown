#!/usr/bin/env python3
"""Host-only CRI fencing inspection and scoped stop operation.

No signed writer_stopped evidence is issued here: CRI stop and a momentary
kernel observation do not prove durable volume isolation or prevent restart.
Run on a trusted Linux node, not inside a tenant container.
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

from recovery_crypto import RecoveryError, decode, digest, read, verify
from recovery_witness import TENANT, identifier, require
from writer_fence import identity, CLOCK_SKEW
from witness_client import Client

UID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
DEVICE = re.compile(r"[0-9]+:[0-9]+\Z")
CID = re.compile(r"[a-f0-9]{64}\Z")


def configuration(value):
    require(isinstance(value, dict) and set(value) == {"tenant", "role", "identity", "devices"},
            "invalid_host_inventory")
    require(isinstance(value["tenant"], str) and TENANT.fullmatch(value["tenant"])
            and value["role"] in ("sync", "history"), "invalid_host_inventory")
    ids = identity(value["identity"])
    require(all(UID.fullmatch(v) for v in [ids["podUid"], ids["nodeUid"], *ids["pvcUids"]]),
            "expected_kubernetes_uuid")
    devices = value["devices"]
    require(isinstance(devices, dict) and set(devices) == set(ids["pvcUids"])
            and all(isinstance(v, str) and DEVICE.fullmatch(v) and v.split(":")[0] != "0"
                    for v in devices.values()), "invalid_pvc_device_inventory")
    return value


def challenge(envelope, public, cfg, now):
    ch = verify(envelope, public)
    require(isinstance(ch, dict) and set(ch) == {"schema", "kind", "tenant", "incident", "nonce",
            "previousEpoch", "epoch", "writers", "notBeforeUnix", "issuedUnix"}
            and ch["schema"] == 1 and ch["kind"] == "writer_freeze"
            and ch["tenant"] == cfg["tenant"] and identifier(ch["incident"])
            and isinstance(ch["writers"], dict) and set(ch["writers"]) == {"sync", "history"}
            and identity(ch["writers"][cfg["role"]]) == cfg["identity"],
            "freeze_identity_mismatch")
    require(type(ch["epoch"]) is int and type(ch["previousEpoch"]) is int
            and ch["previousEpoch"] > 0 and ch["epoch"] == ch["previousEpoch"] + 1
            and type(ch["issuedUnix"]) is int and type(ch["notBeforeUnix"]) is int
            and ch["issuedUnix"] <= now + CLOCK_SKEW
            and ch["issuedUnix"] <= ch["notBeforeUnix"], "invalid_freeze_epoch_or_time")
    return ch


class Cri:
    def __init__(self, socket):
        p = Path(socket)
        require(p.is_absolute() and stat.S_ISSOCK(p.stat().st_mode), "invalid_local_cri_socket")
        self.endpoint = "unix://" + str(p)

    def run(self, *args):
        result = subprocess.run(["/usr/bin/crictl", "--runtime-endpoint", self.endpoint,
                                 "--timeout", "15s", *args], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, check=True, timeout=20)
        require(len(result.stdout) <= 4 * 1024 * 1024, "runtime_response_too_large")
        return result.stdout

    def list(self):
        pods = decode(self.run("pods", "-o", "json"))
        containers = decode(self.run("ps", "-a", "-o", "json"))
        require(isinstance(pods.get("items"), list) and isinstance(containers.get("containers"), list),
                "runtime_inventory_incomplete")
        return pods["items"], containers["containers"]

    def stop(self, sandbox_id):
        require(CID.fullmatch(sandbox_id), "invalid_sandbox_id")
        self.run("stopp", sandbox_id)


def runtime_view(pods, containers, cfg):
    """Never stop a replacement pod, even if its name matches the old pod."""
    exact, replacement, active = [], [], []
    all_ids = set()
    for pod in pods:
        m = pod["metadata"]
        sid = pod["id"]
        require(isinstance(sid, str) and CID.fullmatch(sid) and sid not in all_ids,
                "invalid_or_duplicate_sandbox_id")
        all_ids.add(sid)
        same_uid = m["uid"] == cfg["identity"]["podUid"]
        same_name = m["namespace"] == cfg["tenant"] and m["name"] == cfg["role"] + "-0"
        if same_uid:
            require(same_name, "runtime_pod_uid_conflict")
            exact.append(sid)
        elif same_name:
            replacement.append(sid)
        if same_uid or same_name:
            require(pod["state"] in ("SANDBOX_READY", "SANDBOX_NOTREADY"), "unknown_sandbox_state")
            if pod["state"] != "SANDBOX_NOTREADY":
                active.append(sid)
    running = 0
    for c in containers:
        sid = c["podSandboxId"]
        labels = c.get("labels") or {}
        # Even if the sandbox vanished between CRI list calls, the container
        # metadata may still identify a writer. Treat every orphan as unknown.
        require(sid in all_ids, "runtime_container_without_sandbox")
        if (sid in exact or sid in replacement
                or labels.get("io.kubernetes.pod.uid") == cfg["identity"]["podUid"]):
            require(sid in exact or sid in replacement, "runtime_container_uid_conflict")
            require(c["state"] in ("CONTAINER_CREATED", "CONTAINER_RUNNING", "CONTAINER_EXITED",
                                  "CONTAINER_UNKNOWN"), "unknown_container_state")
            if c["state"] != "CONTAINER_EXITED":
                running += 1
    return {"exact": sorted(exact), "replacementCount": len(replacement),
            "activeSandboxCount": len(active), "nonExitedContainerCount": running}


def mount_rows(raw, devices):
    """Parse actual Linux mountinfo; report every mount of the PVC filesystem.
    A ro bind on a rw superblock is not durable write revocation.
    """
    matching = writable = 0
    for line in raw.splitlines():
        left, right = line.split(" - ", 1)
        a, b = left.split(), right.split()
        require(len(a) >= 6 and len(b) == 3 and DEVICE.fullmatch(a[2]), "invalid_mountinfo")
        if a[2] not in devices:
            continue
        matching += 1
        mount, superblock = set(a[5].split(",")), set(b[2].split(","))
        require(len(mount & {"ro", "rw"}) == 1 and len(superblock & {"ro", "rw"}) == 1,
                "mount_access_unknown")
        if "rw" in mount or "rw" in superblock:
            writable += 1
    return matching, writable


def kernel_view(proc, cfg):
    """Inspect all visible processes and mount namespaces, never log paths.
    PID churn or inaccessible namespaces is not accepted as a clean sample.
    Even two clean samples cannot rule out detached mounts, raw-device access
    or later writes by newly created pods.
    """
    proc = Path(proc)
    devices = set(cfg["devices"].values())
    uid = cfg["identity"]["podUid"]
    pattern = re.compile(r"pod" + "[-_]".join(re.escape(part) for part in uid.split("-")) +
                         r"(?=\.slice|/|$)")
    namespaces = set()
    mounts = rw = processes = 0
    for p in sorted(proc.iterdir(), key=lambda p: p.name):
        if not p.name.isdecimal():
            continue
        try:
            cg = (p / "cgroup").read_text()
            if pattern.search(cg):
                processes += 1
            ns = (p / "ns/mnt").stat().st_ino
            if ns not in namespaces:
                raw = (p / "mountinfo").read_text()
                a, b = mount_rows(raw, devices)
                # Ensure PID namespace identity did not change mid-sample.
                require((p / "ns/mnt").stat().st_ino == ns, "mount_namespace_changed")
                mounts += a
                rw += b
                namespaces.add(ns)
        except FileNotFoundError:
            # Kernel threads and exiting processes can have no mount namespace.
            # A surviving userspace process without it is ambiguous.
            if p.exists():
                require(not (p / "cmdline").read_bytes(), "process_namespace_unavailable")
    require(bool(namespaces), "no_mount_namespaces_observed")
    return {"writerProcessCount": processes, "mountNamespaceCount": len(namespaces),
            "matchingMountCount": mounts, "potentialWritableMountCount": rw}


def observe(cri, cfg, ch, *, stop=False, kernel=kernel_view, now=time.time):
    cfg = configuration(cfg)
    require(ch["writers"][cfg["role"]] == cfg["identity"], "freeze_identity_mismatch")
    before = runtime_view(*cri.list(), cfg)
    if stop:
        require(int(now()) >= ch["notBeforeUnix"], "prior_permits_not_expired")
        require(before["replacementCount"] == 0, "replacement_writer_present")
        for sid in before["exact"]:
            cri.stop(sid)
    first = runtime_view(*cri.list(), cfg)
    k = kernel("/proc", cfg)
    last = runtime_view(*cri.list(), cfg)
    # Any runtime churn is ambiguous, and observations are never a proof.
    require(first == last, "runtime_changed_during_observation")
    blockers = ["durable_restart_prevention_unverified", "durable_storage_isolation_unverified"]
    if last["replacementCount"] or last["activeSandboxCount"] or last["nonExitedContainerCount"] or k["writerProcessCount"]:
        blockers.append("writer_activity_observed")
    if k["potentialWritableMountCount"]:
        blockers.append("potential_writable_mount_observed")
    return {"schema": 1, "kind": "writer_runtime_observation", "tenant": cfg["tenant"],
            "role": cfg["role"], "identity": cfg["identity"], "epoch": ch["epoch"],
            "challengeDigest": digest(ch), "observedUnix": int(now()),
            "runtime": {k: v for k, v in last.items() if k != "exact"}, "kernel": k,
            "stopRequested": stop, "blockers": blockers, "activationAllowed": False,
            "stopEvidenceEligible": False}


def verify_active_freeze(witness, public, cfg, expected, now):
    current = challenge(witness.call({"tenant": cfg["tenant"], "action": "resume-freeze"}),
                        public, cfg, now)
    require(digest(current) == digest(expected), "freeze_not_current")
    return current


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("inventory", "freeze-challenge", "witness-public", "cri-socket"):
        p.add_argument("--" + name, required=True)
    for name in ("witness-endpoint", "witness-ca", "witness-cert", "witness-tls-key"):
        p.add_argument("--" + name)
    p.add_argument("--stop-enrolled", action="store_true",
                   help="Stop exact enrolled sandbox only; never use for automatic live cutover.")
    a = p.parse_args()
    try:
        require(os.geteuid() == 0, "trusted_host_root_required")
        for path in (a.inventory, a.witness_public):
            s = Path(path).lstat()
            require(stat.S_ISREG(s.st_mode) and s.st_uid == 0 and not s.st_mode & 0o022,
                    "host_trust_file_not_root_owned")
        cfg = configuration(decode(read(a.inventory)))
        ch = challenge(decode(read(a.freeze_challenge)), a.witness_public, cfg, int(time.time()))
        if a.stop_enrolled:
            require(all((a.witness_endpoint, a.witness_ca, a.witness_cert, a.witness_tls_key)),
                    "online_witness_required_for_runtime_stop")
            verify_active_freeze(Client(a.witness_endpoint, a.witness_ca, a.witness_cert, a.witness_tls_key),
                                 a.witness_public, cfg, ch, int(time.time()))
        result = observe(Cri(a.cri_socket), cfg, ch, stop=a.stop_enrolled)
        print(json.dumps(result, separators=(",", ":")))
    except (RecoveryError, OSError, ValueError, KeyError, TypeError, AttributeError,
            subprocess.SubprocessError) as exc:
        print(json.dumps({"kind": "writer_runtime_observation", "state": "blocked",
                          "reason": str(exc) if isinstance(exc, RecoveryError) else type(exc).__name__,
                          "activationAllowed": False, "stopEvidenceEligible": False}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
