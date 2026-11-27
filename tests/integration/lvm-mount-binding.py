"""Read-only local PV identity and mount binding tests."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/"server/integrity"), str(ROOT/"server/storage-controller")]
import lvm_mount_binding as m
from recovery_crypto import RecoveryError


def uid():
    return str(uuid.uuid4())


class API:
    def __init__(self, rows):
        self.rows = rows
    def get(self, path):
        return copy.deepcopy(self.rows[path])


class Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.pvc = uid()
        self.path = self.root/self.pvc
        self.path.mkdir()
        tenant = "gtm-"+"a"*32
        self.cfg = {"tenant": tenant, "role": "history", "namespaceUid": uid(), "machineId": "f"*32,
                    "identity": {"podUid": uid(), "nodeUid": uid(), "pvcUids": [self.pvc]},
                    "volumes": {self.pvc: {"vgName": "gtmd-ci", "vgUuid": "ABCDEF-1234-5678-9abc-def0-1234-ABCDEF",
                                           "lvUuid": "FEDCBA-4321-8765-cba9-0fed-4321-FEDCBA"}}}
        ns = "/api/v1/namespaces/"+tenant
        self.nspath = ns
        self.podpath = ns+"/pods/history-0"
        self.claimpath = ns+"/persistentvolumeclaims/history-data"
        self.pvpath = "/api/v1/persistentvolumes/pv-ci"
        self.rows = {
            ns: {"metadata": {"name": tenant, "uid": self.cfg["namespaceUid"], "resourceVersion": "1"},
                 "status": {"phase": "Active"}},
            "/api/v1/nodes/node": {"metadata": {"name": "node", "uid": self.cfg["identity"]["nodeUid"],
                                               "labels": {"kubernetes.io/hostname": "host"}}},
            self.podpath: {"metadata": {"name": "history-0", "uid": self.cfg["identity"]["podUid"],
                                        "namespace": tenant},
                           "spec": {"nodeName": "node", "volumes": [{"persistentVolumeClaim": {"claimName": "history-data"}}]}},
            self.claimpath: {"metadata": {"name": "history-data", "uid": self.pvc, "namespace": tenant},
                             "status": {"phase": "Bound"}, "spec": {"volumeName": "pv-ci",
                                 "accessModes": ["ReadWriteOnce"], "storageClassName": "local"}},
            self.pvpath: {"metadata": {"name": "pv-ci", "uid": uid()}, "status": {"phase": "Bound"},
                          "spec": {"claimRef": {"uid": self.pvc, "name": "history-data", "namespace": tenant},
                                   "persistentVolumeReclaimPolicy": "Retain", "accessModes": ["ReadWriteOnce"],
                                   "storageClassName": "local", "local": {"path": str(self.path)},
                                   "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [
                                       {"key": "kubernetes.io/hostname", "operator": "In", "values": ["host"]}]}]}}}}}
        d = self.path.stat().st_dev
        self.device = str(os.major(d))+":"+str(os.minor(d))
        self.state = {"active": True, "readOnly": False, "activationSkip": False, "device": self.device}
        self.mount = "1 2 "+self.device+" / "+str(self.path)+" rw - ext4 /dev/dm-0 rw\n"

    def bound(self):
        return m.bound_mounts(API(self.rows), self.cfg, "node", self.root)

    def test_exact_api_binding(self):
        self.assertEqual(self.bound(), {self.pvc: self.path})
        self.rows[self.pvpath]["spec"]["local"]["path"] = str(self.root)
        with self.assertRaises(RecoveryError):
            self.bound()

    def test_reject_mismatches(self):
        cases = [(self.pvpath, "spec", "persistentVolumeReclaimPolicy", "Delete"),
                 (self.pvpath, "spec", "nodeAffinity", {}),
                 (self.claimpath, "metadata", "uid", uid()),
                 (self.podpath, "spec", "nodeName", "other"),
                 (self.podpath, "spec", "hostPID", True),
                 (self.podpath, "spec", "containers", [{"securityContext": {"privileged": True}}]),
                 (self.nspath, "metadata", "annotations", {m.MARKER: "a"*64})]
        for path, section, key, value in cases:
            old = copy.deepcopy(self.rows)
            self.rows[path][section][key] = value
            with self.assertRaises(RecoveryError):
                self.bound()
            self.rows = old

    def test_exact_mount_not_fallback_or_stack(self):
        self.assertEqual(m.verify_mount(self.path, self.state, self.mount)["filesystem"], "ext4")
        for raw in ("", self.mount*2, self.mount.replace(" / ", " /subdir "),
                    self.mount.replace(" rw ", " ro "), self.mount.replace("ext4", "tmpfs"),
                    self.mount.replace(self.device, "999:1")):
            with self.assertRaises(RecoveryError):
                m.verify_mount(self.path, self.state, raw)
        with self.assertRaises(RecoveryError):
            m.verify_mount(self.path, {**self.state, "readOnly": True}, self.mount)


if __name__ == "__main__":
    unittest.main()
