import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server/integrity"))
import host_runtime_fence as f
import recovery_crypto as c

TENANT = "gtm-" + "a" * 32
POD = "01234567-89ab-cdef-0123-456789abcdef"
NEW = "11234567-89ab-cdef-0123-456789abcdef"
PVC = "21234567-89ab-cdef-0123-456789abcdef"
NODE = "31234567-89ab-cdef-0123-456789abcdef"
SID, NEW_SID = "a" * 64, "b" * 64
CFG = {"tenant": TENANT, "role": "sync",
       "identity": {"podUid": POD, "pvcUids": [PVC], "nodeUid": NODE},
       "devices": {PVC: "8:1"}}
CH = {"schema": 1, "kind": "writer_freeze", "tenant": TENANT, "incident": "incident-1",
      "nonce": "c" * 64, "previousEpoch": 1, "epoch": 2,
      "writers": {"sync": CFG["identity"], "history": {"podUid": NEW, "nodeUid": NODE, "pvcUids": [PVC]}},
      "issuedUnix": 100, "notBeforeUnix": 165}


def pod(uid=POD, sid=SID, state="SANDBOX_READY"):
    return {"id": sid, "metadata": {"uid": uid, "name": "sync-0", "namespace": TENANT},
            "state": state}


def container(sid=SID, state="CONTAINER_RUNNING"):
    return {"podSandboxId": sid, "state": state}


class Runtime:
    def __init__(self):
        self.pods = [pod()]
        self.containers = [container()]
        self.stops = []

    def list(self):
        return copy.deepcopy((self.pods, self.containers))

    def stop(self, sid):
        self.stops.append(sid)
        self.pods = [pod(state="SANDBOX_NOTREADY")]
        self.containers = [container(state="CONTAINER_EXITED")]


def kernel(_, cfg):
    return {"writerProcessCount": 0, "mountNamespaceCount": 1,
            "matchingMountCount": 0, "potentialWritableMountCount": 0}


class Tests(unittest.TestCase):
    def test_signed_challenge_and_exact_identity(self):
        with tempfile.TemporaryDirectory() as d:
            private, public = Path(d) / "key", Path(d) / "pub"
            subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private)], check=True)
            subprocess.run(["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)], check=True)
            envelope = c.sign(CH, private)
            self.assertEqual(f.challenge(envelope, public, CFG, 165), CH)
            other = copy.deepcopy(CFG)
            other["identity"]["podUid"] = NEW
            with self.assertRaises(c.RecoveryError):
                f.challenge(envelope, public, other, 165)
            envelope["payload"] = {**CH, "epoch": 3}
            with self.assertRaises(c.RecoveryError):
                f.challenge(envelope, public, CFG, 165)

    def test_observation_and_scoped_stop_never_authorize(self):
        cri = Runtime()
        report = f.observe(cri, CFG, CH, kernel=kernel, now=lambda: 165)
        self.assertIn("writer_activity_observed", report["blockers"])
        self.assertEqual(cri.stops, [])
        with self.assertRaises(c.RecoveryError):
            f.observe(cri, CFG, CH, stop=True, kernel=kernel, now=lambda: 164)
        self.assertEqual(cri.stops, [])
        report = f.observe(cri, CFG, CH, stop=True, kernel=kernel, now=lambda: 165)
        self.assertEqual(cri.stops, [SID])
        self.assertNotIn("writer_activity_observed", report["blockers"])
        self.assertFalse(report["activationAllowed"])
        self.assertFalse(report["stopEvidenceEligible"])
        self.assertNotEqual(report["kind"], "writer_stopped")
        self.assertIn("durable_storage_isolation_unverified", report["blockers"])

    def test_replacement_or_unknown_runtime_is_never_stopped(self):
        cri = Runtime()
        cri.pods.append(pod(NEW, NEW_SID))
        with self.assertRaises(c.RecoveryError):
            f.observe(cri, CFG, CH, stop=True, kernel=kernel, now=lambda: 165)
        self.assertEqual(cri.stops, [])
        cri.pods = []
        with self.assertRaises(c.RecoveryError):
            f.observe(cri, CFG, CH, kernel=kernel, now=lambda: 165)
        cri.pods = [pod(state="SANDBOX_NOTREADY")]
        cri.containers = [container(state="CONTAINER_UNKNOWN")]
        report = f.observe(cri, CFG, CH, kernel=kernel, now=lambda: 165)
        self.assertIn("writer_activity_observed", report["blockers"])

    def test_runtime_change_during_scan_is_rejected(self):
        cri = Runtime()
        def changing(*args):
            cri.pods.append(pod(NEW, NEW_SID))
            return kernel(*args)
        with self.assertRaises(c.RecoveryError):
            f.observe(cri, CFG, CH, kernel=changing, now=lambda: 165)

    def test_mount_parser_and_real_kernel_mountinfo(self):
        raw = ("36 25 8:1 / /private ro - ext4 /dev/sda1 rw\n"
               "37 25 8:1 / /other ro - ext4 /dev/sda1 ro\n"
               "38 25 8:2 / /unrelated rw - ext4 /dev/sda2 rw\n")
        self.assertEqual(f.mount_rows(raw, {"8:1"}), (2, 1))
        actual = Path("/proc/self/mountinfo").read_text()
        dev = actual.splitlines()[0].split()[2]
        count, writable = f.mount_rows(actual, {dev})
        self.assertGreaterEqual(count, 1)
        self.assertLessEqual(writable, count)

    def test_kernel_cgroup_and_mount_namespace_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for pid, cg in [(100, "0::/kubepods.slice/pod" + POD.replace("-", "_") + ".slice\n"),
                            (200, "0::/other\n")]:
                p = root / str(pid)
                (p / "ns").mkdir(parents=True)
                (p / "ns/mnt").touch()
                (p / "cgroup").write_text(cg)
                (p / "mountinfo").write_text("36 25 8:1 / /private ro - ext4 /dev/sda1 rw\n")
            report = f.kernel_view(root, CFG)
            self.assertEqual(report["writerProcessCount"], 1)
            self.assertEqual(report["mountNamespaceCount"], 2)
            self.assertEqual(report["potentialWritableMountCount"], 2)

    def test_missing_pvc_mapping_rejected(self):
        with self.assertRaises(c.RecoveryError):
            f.configuration({**CFG, "devices": {}})


if __name__ == "__main__":
    unittest.main()
