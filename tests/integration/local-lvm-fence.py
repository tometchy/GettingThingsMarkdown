"""Fail-closed tests of local LVM isolation without touching real devices."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "server/integrity"), str(ROOT / "server/storage-controller")]
import local_lvm_fence as f
from recovery_crypto import RecoveryError, digest
from unittest.mock import patch

PVC = "11111111-1111-4111-8111-111111111111"
PVC2 = "22222222-2222-4222-8222-222222222222"
TENANT = "gtm-" + "a"*32
VGID = "ABCDEF-1234-5678-9abc-def0-1234-ABCDEF"
LVID = "FEDCBA-4321-8765-cba9-0fed-4321-FEDCBA"
CFG = {"tenant": TENANT, "role": "sync", "namespaceUid": "33333333-3333-4333-8333-333333333333",
       "machineId": "f"*32,
       "identity": {"podUid": "44444444-4444-4444-8444-444444444444",
                    "nodeUid": "55555555-5555-4555-8555-555555555555",
                    "pvcUids": [PVC, PVC2]},
       "volumes": {PVC: {"vgName": "gtmd-test", "vgUuid": VGID, "lvUuid": LVID},
                   PVC2: {"vgName": "gtmd-test", "vgUuid": VGID,
                          "lvUuid": LVID[:-1]+"C"}}}
CH = {"epoch": 2, "notBeforeUnix": 100}
RW = {"active": True, "open": False, "readOnly": False, "activationSkip": False, "device": "253:1"}
RO = {"active": False, "open": False, "readOnly": True, "activationSkip": True, "device": None}


def row():
    return {"vg_name": "gtmd-test", "vg_uuid": VGID, "lv_name": "pvc-"+PVC, "lv_uuid": LVID,
            "lv_attr": "-wi-a-----", "lv_kernel_major": "253", "lv_kernel_minor": "1",
            "lv_tags": "gtmd.tenant."+TENANT+",gtmd.role.sync,gtmd.pvc."+PVC,
            "segtype": "linear", "seg_count": "1"}


class FakeLvm:
    def __init__(self):
        self.states = {PVC: dict(RW), PVC2: {**RW, "device": "253:2"}}
        self.calls = []
        self.fail = None
    def inspect(self, cfg, pvc):
        return dict(self.states[pvc])
    def restrict(self, cfg, pvc):
        self.calls.append(("restrict", pvc))
        if self.fail == pvc:
            raise RecoveryError("simulated_io_error")
        self.states[pvc].update(readOnly=True, activationSkip=True)
    def deactivate(self, cfg, pvc):
        self.calls.append(("deactivate", pvc))
        self.states[pvc].update(active=False, device=None)


class Cri:
    def list(self):
        return [], []


def kernel(*args):
    return {"writerProcessCount": 0, "matchingMountCount": 0}


class Tests(unittest.TestCase):
    def setUp(self):
        self.lvm = FakeLvm()
        self.cfg = copy.deepcopy(CFG)
        self.freeze = patch.object(f, "current_freeze", return_value=CH)
        self.freeze.start()
        self.addCleanup(self.freeze.stop)
    def call(self, **kwargs):
        args = dict(apply=True, now=lambda: 100, kernel=kernel)
        args.update(kwargs)
        return f.reconcile(self.lvm, Cri(), None, None, None, self.cfg, CFG["machineId"], **args)
    def test_all_volumes_and_idempotence(self):
        self.assertEqual(self.call(apply=False)["state"], "isolation_required")
        self.assertFalse(self.lvm.calls)
        result = self.call()
        self.assertEqual(result["isolatedVolumeCount"], 2)
        self.assertFalse(result["activationAllowed"])
        self.assertFalse(result["stopEvidenceEligible"])
        self.assertEqual(self.lvm.calls, [("restrict", PVC), ("deactivate", PVC),
                                          ("restrict", PVC2), ("deactivate", PVC2)])
        self.call()
        self.assertEqual(len(self.lvm.calls), 4)
    def test_deadline_open_mount_process_and_incomplete_inventory(self):
        for kwargs in ({"now": lambda: 99}, {"kernel": lambda *a: {"writerProcessCount": 1, "matchingMountCount": 0}},
                       {"kernel": lambda *a: {"writerProcessCount": 0, "matchingMountCount": 1}}):
            with self.assertRaises(RecoveryError):
                self.call(**kwargs)
        self.lvm.states[PVC2]["open"] = True
        with self.assertRaises(RecoveryError):
            self.call()
        self.lvm.states[PVC2]["open"] = False
        del self.cfg["volumes"][PVC2]
        with self.assertRaises(RecoveryError):
            self.call()
        self.assertFalse(self.lvm.calls)
    def test_partial_failure_never_rolls_back_readonly(self):
        self.lvm.fail = PVC2
        with self.assertRaises(RecoveryError):
            self.call()
        self.assertEqual(self.lvm.states[PVC], RO)
        self.lvm.fail = None
        self.assertEqual(self.call()["isolatedVolumeCount"], 2)
        self.assertEqual(self.lvm.calls.count(("restrict", PVC)), 1)
    def test_stale_freeze_and_wrong_machine(self):
        with patch.object(f, "current_freeze", side_effect=[CH, {**CH, "epoch": 3}]):
            with self.assertRaises(RecoveryError):
                self.call()
        with self.assertRaises(RecoveryError):
            f.reconcile(self.lvm, Cri(), None, None, None, self.cfg, "0"*32, apply=True)
        self.assertFalse(self.lvm.calls)
    def test_shared_lv_rejected(self):
        self.cfg["volumes"][PVC2]["lvUuid"] = LVID
        with self.assertRaises(RecoveryError):
            self.call()
        self.assertFalse(self.lvm.calls)
    def test_validate_lvm_identity_and_kernel(self):
        mapped = [{"dev": "253:1", "ro": "0", "holders": 0}]
        self.assertEqual(f.volume_state(row(), mapped, CFG, PVC), RW)
        for key, value in (("lv_uuid", "wrong"), ("lv_tags", ""), ("segtype", "thin"),
                           ("seg_count", "2"), ("lv_attr", "Vwi-a-t---"),
                           ("lv_attr", "-ri-a----k"), ("lv_attr", "-wi-s-----")):
            bad = {**row(), key: value}
            with self.assertRaises(RecoveryError):
                f.volume_state(bad, mapped, CFG, PVC)
        for bad in ([], mapped*2, [{**mapped[0], "holders": 1}], [{**mapped[0], "dev": "253:2"}]):
            with self.assertRaises(RecoveryError):
                f.volume_state(row(), bad, CFG, PVC)
        stopped = {**row(), "lv_attr": "-ri------k", "lv_kernel_major": "-1", "lv_kernel_minor": "-1"}
        self.assertEqual(f.volume_state(stopped, [], CFG, PVC), RO)
        with self.assertRaises(RecoveryError):
            f.volume_state(stopped, mapped, CFG, PVC)
    def test_sysfs_device_uuid_not_device_number(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"dm-3"
            (p/"dm").mkdir(parents=True)
            (p/"holders").mkdir()
            (p/"dm/uuid").write_text(f.dm_uuid(CFG["volumes"][PVC]))
            (p/"dev").write_text("253:3")
            (p/"ro").write_text("1")
            self.assertEqual(f.mapped_devices(d, CFG["volumes"][PVC]),
                             [{"dev": "253:3", "ro": "1", "holders": 0}])
            self.assertEqual(f.mapped_devices(d, CFG["volumes"][PVC2]), [])
    def test_namespace_marker_required(self):
        class Kube:
            def get(self, path):
                return {"metadata": {"name": TENANT, "uid": CFG["namespaceUid"],
                                     "resourceVersion": "7", "annotations": {f.MARKER: digest(CH)}},
                        "status": {"phase": "Active"}}
        class Witness:
            def call(self, request):
                return {}
        # Exercise the real function rather than the reconcile fixture.
        self.freeze.stop()
        self.freeze = patch.object(f, "challenge", return_value=CH)
        self.freeze.start()
        self.addCleanup(self.freeze.stop)
        self.assertEqual(f.current_freeze(Witness(), Kube(), None, CFG, lambda: 100), CH)
        with patch.object(f, "namespace_state", return_value=({}, {})):
            with self.assertRaises(RecoveryError):
                f.current_freeze(Witness(), Kube(), None, CFG, lambda: 100)


if __name__ == "__main__":
    unittest.main()
