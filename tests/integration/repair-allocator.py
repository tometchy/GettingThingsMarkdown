import copy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/"server/integrity"), str(ROOT/"server/storage-controller")]
import repair_allocator as a
from recovery_crypto import RecoveryError, digest
from kube_client import ApiError
spec = importlib.util.spec_from_file_location("fixtures", ROOT/"tests/integration/storage-controller.py")
fx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fx)

TENANT = fx.TENANT
CLASS = "gtmd-repair-local"
CH = {"epoch": 2, "tenant": TENANT, "incident": "incident",
      "writers": {"sync": {"nodeUid": "n1-uid", "podUid": "sync-pod", "pvcUids": ["claim-uid"]},
                  "history": {"nodeUid": "n3-uid", "podUid": "history-pod", "pvcUids": ["history-uid"]}}}


def spare(node, disk, name, cap="12Gi"):
    p = fx.pv(node, disk, name)
    p["metadata"]["labels"] = {a.SPARE: "true"}
    p["spec"].update(storageClassName=CLASS, capacity={"storage": cap}, accessModes=["ReadWriteOnce"])
    return p


class API:
    def __init__(self):
        old = fx.pv("n1", fx.D1, "occupied", True)
        old["spec"].update(capacity={"storage": "10Gi"}, accessModes=["ReadWriteOnce"])
        self.pvs = [old, spare("n2", fx.D2, "large", "20Gi"), spare("n2", fx.D2, "small"),
                    spare("n2", fx.D2, "too-small", "9Gi"), spare("n3", fx.D3, "other-cloud")]
        self.claim = None
        self.posts = []
        self.fail = None
        self.reports = {n: fx.report(n,d) for n,d in [("n1",fx.D1),("n2",fx.D2),("n3",fx.D3)]}
    def get(self, path):
        name = path.split("/")[-1]
        if "/storageclasses/" in path:
            return {"provisioner": "kubernetes.io/no-provisioner", "volumeBindingMode": "Immediate"}
        if "/nodes/" in path:
            return fx.live(name)
        if "/configmaps/" in path:
            return self.reports[name]
        if "/persistentvolumes/" in path:
            return copy.deepcopy(next(p for p in self.pvs if p["metadata"]["name"] == name))
        if name == "sync-data":
            return copy.deepcopy(fx.PVC)
        if self.claim and name == self.claim["metadata"]["name"]:
            return copy.deepcopy(self.claim)
        raise ApiError(404)
    def list(self, path):
        assert path == "/api/v1/persistentvolumes"
        return iter(copy.deepcopy(self.pvs))
    def request(self, method, path, value):
        assert method == "POST" and path == "/api/v1/namespaces/gtm-recovery/persistentvolumeclaims"
        self.posts.append(value)
        self.claim = copy.deepcopy(value)
        self.claim["metadata"]["uid"] = "target-uid"
        self.claim["status"] = {"phase": "Pending"}
        if self.fail:
            raise self.fail
    def bind(self):
        pv = next(p for p in self.pvs if p["metadata"]["name"] == self.claim["spec"]["volumeName"])
        pv["spec"]["claimRef"] = {"namespace": a.NAMESPACE, "name": self.claim["metadata"]["name"],
                                  "uid": self.claim["metadata"]["uid"]}
        pv["status"]["phase"] = self.claim["status"]["phase"] = "Bound"


class Witness:
    def __init__(self):
        self.plan = None
        self.calls = []
    def call(self, request):
        assert request["action"] == "plan-repair"
        self.calls.append(request)
        self.plan = {"targetRole": request["targetRole"], "targetPvcUid": request["targetPvcUid"],
                     "generation": 1, "anchorDigest": "anchor"}


class Tests(unittest.TestCase):
    def setUp(self):
        self.api = API()
        self.w = Witness()
        self.config = a.inventory({**fx.RAW, "storageClasses": ["gtmd-local", CLASS]})
        self.gen = 1
        self.freeze = patch.object(a, "freeze_status", side_effect=lambda *args: (
            CH, {"generation": self.gen, "anchorDigest": "anchor",
                 "writerFence": {"freeze": {"plan": self.w.plan}}}))
        self.freeze.start()
        self.addCleanup(self.freeze.stop)
    def call(self, apply=True):
        return a.reconcile(self.api, self.w, None, self.config, CLASS, TENANT, "incident",
                           "sync", apply=apply, now=lambda: fx.NOW)
    def test_best_fit_and_one_binding_no_pv_mutation(self):
        self.assertEqual(self.call(False)["candidatePV"], "small")
        self.assertEqual(self.api.posts, [])
        self.assertEqual(self.call()["state"], "waiting_for_binding")
        self.assertEqual(self.call()["state"], "waiting_for_binding")
        self.assertEqual(len(self.api.posts), 1)
        self.assertFalse(self.w.calls)
        self.api.bind()
        self.assertEqual(self.call(False)["state"], "binding_verified")
        self.assertEqual(self.call()["state"], "allocated")
        self.assertEqual(self.call()["state"], "allocated")
        self.assertEqual(len(self.w.calls), 1)
        self.assertFalse(self.call()["physicalMountVerified"])
    def test_lost_reply_recovers_same_claim(self):
        self.api.fail = OSError("lost POST reply")
        with self.assertRaises(OSError):
            self.call()
        self.api.fail = None
        self.assertEqual(self.call()["state"], "waiting_for_binding")
        self.assertEqual(len(self.api.posts), 1)
    def test_no_capacity_no_anchor_and_stale_health(self):
        self.gen = 0
        self.assertEqual(self.call()["state"], "waiting_for_anchor")
        self.gen = 1
        self.api.reports["n2"] = fx.report("n2",fx.D2,ts=fx.NOW-301)
        self.assertEqual(self.call()["state"], "waiting_for_capacity")
        self.assertFalse(self.api.posts)
    def test_no_reuse_and_no_retarget(self):
        self.call()
        self.api.bind()
        self.api.claim["metadata"]["annotations"][a.PREFIX+"target-pv-uid"] = "substituted"
        with self.assertRaises(RecoveryError):
            self.call()
        self.assertFalse(self.w.calls)
    def test_target_conflict_does_not_delete_or_reallocate(self):
        self.call()
        self.api.pvs[2]["spec"]["claimRef"] = {"namespace": "other", "name": "other", "uid": "other"}
        with self.assertRaises(RecoveryError):
            self.call()
        self.assertEqual(len(self.api.posts), 1)
        self.assertFalse(self.w.calls)
    def test_duplicate_path_is_not_eligible(self):
        self.api.pvs[1]["spec"]["local"]["path"] = self.api.pvs[2]["spec"]["local"]["path"]
        self.assertEqual(self.call()["state"], "waiting_for_capacity")
        self.assertFalse(self.api.posts)


if __name__ == "__main__":
    unittest.main()
