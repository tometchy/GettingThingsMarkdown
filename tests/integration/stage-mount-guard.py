"""Actual stage target identity checks with synthetic API and mountinfo."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/"server/integrity"), str(ROOT/"server/storage-controller")]
import stage_mount_guard as g
from recovery_crypto import RecoveryError
from controller import inventory


def uid():
    return str(uuid.uuid4())


class Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.mount = self.root/"slot"
        self.mount.mkdir()
        self.cfg = {"tenant": "gtm-"+"a"*32, "role": "sync", "incident": "incident",
                    "machineId": "a"*32, "nodeName": "node", "pvName": "pv-slot", "pvUid": uid(),
                    "pvcUid": uid(), "mount": str(self.mount), "vgName": "gtmd-test",
                    "vgUuid": "ABCDEF-1234-5678-9abc-def0-1234-ABCDEF",
                    "lvUuid": "FEDCBA-4321-8765-cba9-0fed-4321-FEDCBA"}
        node_uid = uid()
        self.storage = inventory({"storageClasses": ["repair"], "nodes": [
            {"name": "node", "uid": node_uid, "hostname": "host", "cloud": "a",
             "disks": [{"id":"b"*24,"mount":str(self.root)}]}]})
        self.pv = {"metadata": {"name": self.cfg["pvName"], "uid": self.cfg["pvUid"],
                                "annotations": {"gtmd.io/node":"node","gtmd.io/node-uid":node_uid,
                                                "gtmd.io/disk-id":"b"*24}},
                   "spec": {"local":{"path": str(self.mount)},
                            "claimRef":{"uid":self.cfg["pvcUid"]},
                            "nodeAffinity":{"required":{"nodeSelectorTerms":[{"matchExpressions":[
                                {"key":"kubernetes.io/hostname","operator":"In","values":["host"]}]}]}}},
                   "status":{"phase":"Bound"}}
        self.identity = {"tenant":self.cfg["tenant"],"generation":1,"target_role":"sync",
                         "target_cloud":"a","target_pvc_uid":self.cfg["pvcUid"]}
        self.status = {"generation":1,"writerFence":{"phase":"frozen"},
                       "repair":{"operation":"op","targetPvcUid":self.cfg["pvcUid"],
                                 "targetRole":"sync","state":"ticket_issued"}}
        self.allocation = {"state":"allocated","targetPvcUid":self.cfg["pvcUid"],
                           "targetPV":self.cfg["pvName"],"activationAllowed":False}
        d = self.mount.stat().st_dev
        self.device = str(os.major(d))+":"+str(os.minor(d))
        self.mountinfo = self.root/"mountinfo"
        self.mountinfo.write_text("1 2 "+self.device+" / "+str(self.mount)+" rw - ext4 /dev/dm-1 rw\n")
        self.state = {"active":True,"readOnly":False,"activationSkip":False,"device":self.device}
        self.calls = []
        test = self
        class Kube:
            def get(self,path):
                assert path=="/api/v1/persistentvolumes/"+test.cfg["pvName"]
                return copy.deepcopy(test.pv)
        class Witness:
            def call(self,request):
                assert request["action"]=="status"
                return copy.deepcopy(test.status)
        class Lvm:
            def inspect(self,cfg,pvc):
                assert pvc==test.cfg["pvcUid"] and cfg["tenant"]==test.cfg["tenant"]
                assert cfg["volumes"][pvc]["lvUuid"]==test.cfg["lvUuid"]
                return test.state
        self.kube,self.witness,self.lvm=Kube(),Witness(),Lvm()
        def allocate(*args,**kwargs):
            self.calls.append(kwargs)
            return self.allocation
        patcher = patch.object(g,"inspect_allocation",side_effect=allocate)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self,machine=None):
        return g.verify_binding(self.kube,self.witness,None,self.storage,"repair",self.cfg,
                                self.identity,"op",self.lvm,machine or self.cfg["machineId"],self.mountinfo)

    def test_read_only_reuse_of_allocator_and_mount_check(self):
        self.assertEqual(self.call(),{"mount":str(self.mount),"device":self.device,"filesystem":"ext4"})
        self.assertEqual(self.calls,[{"apply":False}])

    def test_wrong_host_or_target_never_touches_allocator(self):
        with self.assertRaises(RecoveryError):
            self.call("b"*32)
        self.identity["target_pvc_uid"]=uid()
        with self.assertRaises(RecoveryError):
            self.call()
        self.assertEqual(self.calls,[])

    def test_pending_allocation_or_wrong_operation_rejected(self):
        self.allocation["state"]="waiting_for_binding"
        with self.assertRaises(RecoveryError):
            self.call()
        self.allocation["state"]="allocated"
        self.status["repair"]["operation"]="other"
        with self.assertRaises(RecoveryError):
            self.call()

    def test_reject_pv_substitution_and_wrong_cloud(self):
        original=self.pv["metadata"]["uid"]
        self.pv["metadata"]["uid"]=uid()
        with self.assertRaises(RecoveryError):
            self.call()
        self.pv["metadata"]["uid"]=original
        self.identity["target_cloud"]="b"
        with self.assertRaises(RecoveryError):
            self.call()

    def test_missing_or_readonly_mount_rejected(self):
        self.mountinfo.write_text("")
        with self.assertRaises(RecoveryError):
            self.call()


if __name__=="__main__":
    unittest.main()
