import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server/storage-controller"))
import controller as c
import publish_report as p
from kube_client import ApiError

NOW = 10000
D1 = "a" * 24
D2 = "b" * 24
D3 = "c" * 24
TENANT = "gtm-" + "a" * 32


def node(n, disk, cloud="a"):
    return {"name": n, "uid": n + "-uid", "hostname": n, "cloud": cloud,
            "disks": [{"id": disk, "mount": "/mnt/" + n}]}


RAW = {"storageClasses": ["gtmd-local"], "nodes": [
    node("n1", D1), node("n2", D2), node("n3", D3, "b")]}


def live(n):
    return {"metadata": {"name": n, "uid": n + "-uid", "labels": {"kubernetes.io/hostname": n}},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]}}


def report(n, disk, state="healthy", ts=NOW):
    return {"metadata": {"name": n, "annotations": {"gtmd.io/node-uid": n + "-uid"}, "resourceVersion": "1"},
            "data": {"report.json": json.dumps({"node": n, "nodeUid": n + "-uid",
                "inventoryOk": True, "checkedUnix": ts,
                "disks": [{"id": disk, "state": state, "checkedUnix": ts}]})}}


def pv(n, disk, vol, bound=False):
    spec = {"storageClassName": "gtmd-local", "persistentVolumeReclaimPolicy": "Retain",
            "local": {"path": "/mnt/" + n + "/" + vol},
            "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [
                {"key": "kubernetes.io/hostname", "operator": "In", "values": [n]}]}]}}}
    if bound:
        spec["claimRef"] = {"name": "sync-data", "namespace": TENANT, "uid": "claim-uid"}
    return {"metadata": {"name": vol, "uid": vol + "-uid", "resourceVersion": "1",
        "annotations": {"gtmd.io/node": n, "gtmd.io/node-uid": n + "-uid", "gtmd.io/disk-id": disk}},
        "spec": spec, "status": {"phase": "Bound" if bound else "Available"}}


PVC = {"metadata": {"name": "sync-data", "namespace": TENANT, "uid": "claim-uid"},
       "spec": {"volumeName": "occupied"}, "status": {"phase": "Bound"}}


class Fake:
    def __init__(self):
        self.nodes = [live("n1"), live("n2"), live("n3")]
        self.reports = [report("n1", D1, "suspect"), report("n2", D2), report("n3", D3)]
        self.pvs = [pv("n1", D1, "free"), pv("n1", D1, "occupied", True),
                    pv("n2", D2, "candidate"), pv("n3", D3, "other-cloud")]
        self.patches = []
        self.conflict = False

    def list(self, path):
        return iter({"/api/v1/nodes": self.nodes,
                     "/api/v1/namespaces/gtm-storage-reports/configmaps": self.reports,
                     "/api/v1/persistentvolumes": self.pvs,
                     "/api/v1/persistentvolumeclaims": [PVC]}[path])

    def get(self, path):
        return self.reports[0]

    def patch(self, path, ops):
        self.patches.append((path, ops))
        if self.conflict:
            raise ApiError(409)


class Tests(unittest.TestCase):
    def setUp(self):
        self.config = c.inventory(RAW)
        self.api = Fake()

    def plan(self):
        return c.plan(self.config, self.api.nodes, self.api.reports,
                      self.api.pvs, [PVC], NOW)

    def test_suspect_mapping_and_safe_candidates(self):
        states, rows = self.plan()
        by = {x["pv"]: x for x in rows}
        self.assertEqual(states["n1"][D1], "suspect")
        self.assertEqual(by["free"]["action"], "reserve_available")
        self.assertEqual(by["occupied"]["action"], "repair_required")
        self.assertEqual(by["occupied"]["requiredSourceRole"], "history")
        self.assertEqual(by["occupied"]["candidatePVs"], ["candidate"])
        self.assertIn("fencing", by["occupied"]["repairBlockedBy"])

    def test_read_only_default_and_cas_reservation(self):
        c.reconcile(self.api, self.config, now=NOW)
        self.assertFalse(self.api.patches)
        c.reconcile(self.api, self.config, apply=True, now=NOW)
        self.assertEqual(len(self.api.patches), 1)
        path, patch = self.api.patches[0]
        self.assertTrue(path.endswith("/free"))
        self.assertIn({"op": "test", "path": "/metadata/resourceVersion", "value": "1"}, patch)
        self.assertEqual(patch[-1]["value"]["namespace"], c.RESERVATION_NAMESPACE)
        self.assertNotIn("occupied", path)

    def test_conflict_never_retries_stale_patch(self):
        self.api.conflict = True
        result = c.reconcile(self.api, self.config, apply=True, now=NOW)
        self.assertEqual(len(self.api.patches), 1)
        self.assertEqual(result["manualReview"], 1)

    def test_missing_stale_or_replaced_node_is_unknown(self):
        for reports in ([], [report("n1", D1, ts=NOW-301)], [report("n1", D2)]):
            self.api.reports = reports
            self.assertEqual(self.plan()[0]["n1"][D1], "unknown")
        self.api.reports = [report("n1", D1)]
        self.api.nodes[0]["metadata"]["uid"] = "different-uid"
        self.assertEqual(self.plan()[0]["n1"][D1], "unknown")

    def test_invalid_pv_mapping_fails_closed(self):
        self.api.pvs[0]["spec"]["local"]["path"] = "/mnt/n10/free"
        self.assertIsNone(c.mapping(self.api.pvs[0], self.config[1]))
        self.api.pvs[0]["spec"]["local"]["path"] = "/mnt/n1/free"
        self.api.pvs[0]["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"].append({})
        self.assertIsNone(c.mapping(self.api.pvs[0], self.config[1]))

    def test_never_reserve_bound_or_delete_policy(self):
        self.assertIsNone(c.reserve_patch(self.api.pvs[1]))
        self.api.pvs[0]["spec"]["persistentVolumeReclaimPolicy"] = "Delete"
        self.assertIsNone(c.reserve_patch(self.api.pvs[0]))
        self.api.pvs[0]["spec"]["persistentVolumeReclaimPolicy"] = "Retain"
        self.api.pvs[0]["spec"]["claimRef"] = {"name": "already-reserved"}
        self.assertIsNone(c.reserve_patch(self.api.pvs[0]))

    def test_publisher_redacts_serial_and_preserves_checked_time(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "health.json"
            path.write_text(json.dumps({"inventoryOk": True, "checkedUnix": NOW - 5,
                "disks": [{"id": D1, "state": "suspect", "checkedUnix": NOW - 6,
                           "serial": "PRIVATE_SERIAL", "byId": "PRIVATE_PATH"}]}))
            self.assertTrue(p.publish(self.api, path, "n1", "n1-uid", NOW))
            value = self.api.patches[0][1][-1]["value"]["report.json"]
            self.assertNotIn("PRIVATE", value)
            self.assertEqual(json.loads(value)["checkedUnix"], NOW-5)
            self.assertFalse(p.report_value(path, "n1", "n1-uid", NOW+400)["inventoryOk"])
            with self.assertRaises(ValueError):
                p.publish(self.api, path, "n1", "wrong-uid", NOW)

    def test_controller_metrics_fail_closed(self):
        s = c.Status()
        self.assertIn("gtmd_storage_controller_ok 0", s.text(NOW))
        s.value = {"updatedUnix": NOW-1, "boundRepairs": 2, "unhealthyDisks": 1}
        self.assertIn("gtmd_storage_controller_ok 1", s.text(NOW))
        self.assertIn("gtmd_storage_controller_bound_repairs 2", s.text(NOW))
        self.assertIn("gtmd_storage_controller_ok 0", s.text(NOW+301))
        s.value["state"] = "failed"
        self.assertIn("gtmd_storage_controller_ok 0", s.text(NOW))

    def test_reserved_pv_never_auto_released(self):
        self.api.pvs[0]["spec"]["claimRef"] = {"namespace": c.RESERVATION_NAMESPACE,
                                               "name": "quarantine-test"}
        self.api.reports[0] = report("n1", D1)
        self.assertEqual(self.plan()[1][0]["action"], "reserved")


if __name__ == "__main__":
    unittest.main()
