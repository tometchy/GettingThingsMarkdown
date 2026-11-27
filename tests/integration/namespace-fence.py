import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "server/integrity"), str(ROOT / "server/storage-controller")]
import namespace_fence as f
import recovery_crypto as crypto
from kube_client import ApiError


def uid():
    return str(uuid.uuid4())


class FakeKube:
    def __init__(self, cfg):
        self.ns = {"metadata": {"name": cfg["tenant"], "uid": cfg["namespaceUid"],
                                "resourceVersion": "1", "annotations": {"keep": "yes"}},
                   "status": {"phase": "Active"}}
        self.denial = ApiError(422, category="freeze_marker_admission_denied")
        self.writes = 0
        self.probes = 0
        self.conflict = False

    def get(self, path):
        return copy.deepcopy(self.ns)

    def patch(self, path, ops):
        if "?dryRun=All" in path:
            self.probes += 1
            if self.denial:
                raise self.denial
            return
        if self.conflict:
            raise ApiError(409)
        m = self.ns["metadata"]
        assert ops[:2] == [{"op": "test", "path": "/metadata/uid", "value": m["uid"]},
                           {"op": "test", "path": "/metadata/resourceVersion", "value": m["resourceVersion"]}]
        self.writes += 1
        if ops[-1]["path"] == "/metadata/annotations":
            m["annotations"] = ops[-1]["value"]
        else:
            assert ops[-1]["path"] == f.POINTER
            m["annotations"][f.MARKER] = ops[-1]["value"]
        m["resourceVersion"] = str(int(m["resourceVersion"]) + 1)


class Witness:
    def __init__(self, envelope):
        self.envelope = envelope
        self.calls = []

    def call(self, value):
        self.calls.append(value)
        return self.envelope


class Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.key, self.pub = Path(temp.name) / "key", Path(temp.name) / "pub"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(self.key)], check=True)
        subprocess.run(["openssl", "pkey", "-in", str(self.key), "-pubout", "-out", str(self.pub)], check=True)
        self.cfg = {"tenant": "gtm-" + "a"*32, "namespaceUid": uid(), "role": "sync",
                    "identity": {"podUid": uid(), "nodeUid": uid(), "pvcUids": sorted([uid(), uid()])}}
        self.ch = {"schema": 1, "kind": "writer_freeze", "tenant": self.cfg["tenant"],
                   "incident": "incident-1", "nonce": "a"*64, "previousEpoch": 1, "epoch": 2,
                   "issuedUnix": 100, "notBeforeUnix": 165,
                   "writers": {"sync": self.cfg["identity"], "history":
                               {"podUid": uid(), "nodeUid": uid(), "pvcUids": [uid()]}}}
        self.witness = Witness(crypto.sign(self.ch, self.key))
        self.api = FakeKube(self.cfg)

    def run_fence(self, apply=True):
        return f.reconcile(self.api, self.witness, self.pub, self.cfg, apply=apply, now=lambda: 101)

    def test_read_only_and_idempotent_cas_before_permits_expire(self):
        self.assertEqual(self.run_fence(False)["state"], "marker_required")
        self.assertEqual(self.api.writes, 0)
        first = self.run_fence()
        self.assertEqual(first["state"], "marker_present")
        self.assertEqual(self.api.ns["metadata"]["annotations"]["keep"], "yes")
        self.assertEqual(first, self.run_fence())
        self.assertEqual(self.api.writes, 1)
        self.assertEqual(self.api.probes, 2)
        self.assertEqual(self.witness.calls, [{"tenant": self.cfg["tenant"], "action": "resume-freeze"}]*3)
        self.assertFalse(first["activationAllowed"])
        self.assertFalse(first["stopEvidenceEligible"])
        self.assertFalse(first["workloadAdmissionVerified"])

    def test_actor_is_scoped_to_one_tenant_and_no_policy_management(self):
        manifest = f.actor_manifest(self.cfg["tenant"], "gtm-system")
        sa, role, binding = manifest["items"]
        self.assertFalse(sa["automountServiceAccountToken"])
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["namespaces"],
                                         "resourceNames": [self.cfg["tenant"]], "verbs": ["get", "patch"]}])
        self.assertEqual(binding["subjects"][0]["name"], self.cfg["tenant"] + "-fence")
        with self.assertRaises(crypto.RecoveryError):
            f.actor_manifest("default")

    def test_missing_annotation_map(self):
        del self.api.ns["metadata"]["annotations"]
        self.assertEqual(self.run_fence()["state"], "marker_present")
        self.assertEqual(self.api.ns["metadata"]["annotations"], {f.MARKER: crypto.digest(self.ch)})

    def test_policy_absent_generic_denial_or_conflict_never_mutates(self):
        for denial in (None, ApiError(403), ApiError(422), ApiError(409)):
            self.api.denial = denial
            with self.assertRaises(crypto.RecoveryError):
                self.run_fence()
        self.api.denial = ApiError(403, category="freeze_marker_admission_denied")
        self.api.conflict = True
        with self.assertRaises(ApiError):
            self.run_fence()
        self.assertEqual(self.api.writes, 0)

    def test_wrong_namespace_or_prior_marker_never_mutates(self):
        self.api.ns["metadata"]["uid"] = uid()
        with self.assertRaises(crypto.RecoveryError):
            self.run_fence()
        self.api.ns["metadata"]["uid"] = self.cfg["namespaceUid"]
        self.api.ns["metadata"]["annotations"][f.MARKER] = "b"*64
        with self.assertRaises(crypto.RecoveryError):
            self.run_fence()
        self.assertEqual(self.api.probes, 0)

    def test_bad_signature_wrong_role_identity_and_open_witness(self):
        self.witness.envelope["payload"] = {**self.ch, "epoch": 3}
        with self.assertRaises(crypto.RecoveryError):
            self.run_fence()
        self.witness.envelope = crypto.sign(self.ch, self.key)
        self.cfg["role"] = "history"
        with self.assertRaises(crypto.RecoveryError):
            self.run_fence()
        self.cfg["role"] = "sync"
        self.witness.envelope = None
        with self.assertRaises(crypto.RecoveryError):
            self.run_fence()
        self.assertEqual(self.api.writes, 0)


if __name__ == "__main__":
    unittest.main()
