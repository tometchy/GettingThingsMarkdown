#!/usr/bin/env python3
"""Real device-mapper/LVM test on a disposable GitHub-hosted runner.

Two linear LVs share one loop-backed PV. No physical disk, tenant data,
provider API, or existing VG is ever selected by the fixture.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "server/integrity"), str(ROOT / "server/storage-controller")]
import local_lvm_fence as f
from recovery_crypto import RecoveryError, digest, sign


def run(*args, check=True):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if check and p.returncode:
        raise RuntimeError(" ".join(args[:3]) + ": " + p.stderr.decode(errors="replace"))
    return p


def lvm(*args, check=True):
    return run("/usr/sbin/lvm", *args, check=check)


def uid():
    return str(uuid.uuid4())


def lvm_report(vg, lv):
    result = lvm("lvs", "--reportformat", "json", "-o", "vg_uuid,lv_uuid,lv_attr",
                 vg + "/" + lv)
    return json.loads(result.stdout)["report"][0]["lv"][0]


def wait_for_device(path):
    for _ in range(30):
        if path.exists():
            return
        time.sleep(.2)
    raise AssertionError("device did not appear")


def reject(fn, expected):
    try:
        fn()
    except RecoveryError as exc:
        assert str(exc) == expected, str(exc)
    else:
        raise AssertionError("expected rejection: " + expected)


def main():
    assert os.geteuid() == 0 and os.environ.get("GITHUB_ACTIONS") == "true"
    # Restrict fixture setup/cleanup to its own freshly created loop and VG.
    tenant = "gtm-" + uuid.uuid4().hex
    pvc, neighbor = uid(), uid()
    vg = "gtmd-ci-" + uuid.uuid4().hex[:12]
    name = "pvc-" + pvc
    other = "pvc-" + neighbor
    with tempfile.TemporaryDirectory(prefix="gtmd-lvm-ci-") as tmp:
        root = Path(tmp)
        backing = root / "disk"
        with backing.open("wb") as stream:
            stream.truncate(256 * 1024 * 1024)
        mount = root / "mnt"
        mount.mkdir()
        loop = run("losetup", "--find", "--show", str(backing)).stdout.decode().strip()
        made_vg = False
        try:
            assert loop.startswith("/dev/loop")
            lvm("pvcreate", "--yes", loop)
            lvm("vgcreate", vg, loop)
            made_vg = True
            lvm("lvcreate", "-L", "64M", "-n", name, "--addtag", "gtmd.tenant."+tenant,
                "--addtag", "gtmd.role.sync", "--addtag", "gtmd.pvc."+pvc, vg)
            lvm("lvcreate", "-L", "64M", "-n", other, vg)
            dev, neighbor_dev = Path("/dev")/vg/name, Path("/dev")/vg/other
            wait_for_device(dev)
            wait_for_device(neighbor_dev)
            run("mkfs.ext4", "-q", "-F", str(dev))
            run("mount", str(dev), str(mount))
            (mount/"preserve").write_bytes(b"encrypted-test-fixture")
            os.sync()
            # Real witness signature and current marker. Transport and CRI
            # are fixtures here; the previous kind job tests both separately.
            key, public = root/"key", root/"public"
            run("openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key))
            run("openssl", "pkey", "-in", str(key), "-pubout", "-out", str(public))
            row = lvm_report(vg, name)
            cfg = {"tenant": tenant, "role": "sync", "namespaceUid": uid(),
                   "machineId": Path("/etc/machine-id").read_text().strip(),
                   "identity": {"podUid": uid(), "nodeUid": uid(), "pvcUids": [pvc]},
                   "volumes": {pvc: {"vgName": vg, "vgUuid": row["vg_uuid"], "lvUuid": row["lv_uuid"]}}}
            now = int(time.time())
            ch = {"schema": 1, "kind": "writer_freeze", "tenant": tenant, "incident": "ci-lvm",
                  "nonce": "a"*64, "previousEpoch": 1, "epoch": 2, "issuedUnix": now-10,
                  "notBeforeUnix": now-1, "writers": {"sync": cfg["identity"], "history":
                      {"podUid": uid(), "nodeUid": uid(), "pvcUids": [uid()]}}}
            class Witness:
                def call(self, request):
                    assert request == {"tenant": tenant, "action": "resume-freeze"}
                    return sign(ch, key)
            class Kube:
                def get(self, path):
                    assert path == "/api/v1/namespaces/"+tenant
                    return {"metadata": {"name": tenant, "uid": cfg["namespaceUid"],
                                         "resourceVersion": "1", "annotations": {f.MARKER: digest(ch)}},
                            "status": {"phase": "Active"}}
            class Cri:
                def list(self):
                    return [], []
            backend = f.Lvm()
            def reconcile(apply):
                return f.reconcile(backend, Cri(), Kube(), Witness(), public, cfg,
                                   cfg["machineId"], apply=apply)
            assert backend.inspect(cfg, pvc)["open"]
            reject(lambda: reconcile(True), "lvm_volume_still_mounted_or_open")
            assert lvm_report(vg, name)["lv_attr"][1] == "w"
            run("umount", str(mount))
            assert reconcile(False)["state"] == "isolation_required"
            result = reconcile(True)
            assert result["state"] == "isolated" and result["isolatedVolumeCount"] == 1
            assert not result["activationAllowed"] and not result["stopEvidenceEligible"]
            assert reconcile(True)["state"] == "isolated"
            assert not dev.exists()
            # Ordinary activation cannot restore the isolated device.
            lvm("lvchange", "-ay", vg+"/"+name, check=False)
            assert f.isolated(backend.inspect(cfg, pvc))
            # Deliberately override the skip flag ONLY in this fixture.
            # Persistent read-only metadata must still prevent raw writes.
            lvm("lvchange", "-ay", "-K", vg+"/"+name)
            wait_for_device(dev)
            assert run("blockdev", "--getro", str(dev)).stdout.strip() == b"1"
            try:
                with dev.open("r+b", buffering=0) as stream:
                    stream.write(b"not allowed")
            except OSError:
                pass
            else:
                raise AssertionError("isolated LV accepted raw write")
            run("mount", "-o", "ro,noload", str(dev), str(mount))
            assert (mount/"preserve").read_bytes() == b"encrypted-test-fixture"
            run("umount", str(mount))
            lvm("lvchange", "-an", vg+"/"+name)
            assert f.isolated(backend.inspect(cfg, pvc))
            # The other tenant on the same physical loop remains writable.
            assert run("blockdev", "--getro", str(neighbor_dev)).stdout.strip() == b"0"
            with neighbor_dev.open("r+b", buffering=0) as stream:
                stream.write(b"neighbor remains writable")
                stream.seek(0)
                assert stream.read(25) == b"neighbor remains writable"
            print("PASS: real LVM metadata read-only + activation skip + inactive device; mounted refusal, retry and shared-disk neighbor isolation.")
        finally:
            run("umount", str(mount), check=False)
            if made_vg:
                # Cleanup ONLY our uniquely named disposable VG.
                lvm("vgremove", "-f", "-y", vg, check=False)
            lvm("pvremove", "-y", loop, check=False)
            run("losetup", "-d", loop, check=False)


if __name__ == "__main__":
    main()
