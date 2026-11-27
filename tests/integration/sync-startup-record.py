"""Host startup record tests: real Git and signed witness generation, simulated bindings."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/"server/integrity"),str(ROOT/"server/storage-controller")]
import sync_startup_record as rec
from recovery_crypto import RecoveryError, digest, sign
from git_integrity import refs


def uid():
    return str(uuid.uuid4())


class Tests(unittest.TestCase):
    def setUp(self):
        d=tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root=Path(d.name).resolve()
        self.data,self.live=uid(),uid()
        self.paths={u:self.root/u for u in (self.data,self.live)}
        for p in self.paths.values():
            p.mkdir()
        self.repo=self.paths[self.data]/"git"
        subprocess.run(["git","init","-q","--bare",str(self.repo)],check=True)
        def git(*a):
            return subprocess.check_output(["git","--git-dir",str(self.repo),"-c","user.name=CI",
                                            "-c","user.email=ci@example.invalid",*a],input=b"")
        tree=git("mktree").decode().strip()
        commit=git("commit-tree",tree,"-m","test").decode().strip()
        for b in ("main","gtm-tenant","gtm-vault","gtm-mail"):
            git("update-ref","refs/heads/"+b,commit)
        self.manifest=refs(self.repo)
        self.cfg={"tenant":"gtm-"+"a"*32,"role":"sync","namespaceUid":uid(),"machineId":"b"*32,
                  "identity":{"podUid":uid(),"nodeUid":uid(),"pvcUids":sorted(self.paths)},
                  "volumes":{u:{"vgName":"gtmd-test","vgUuid":"ABCDEF-1234-5678-9abc-def0-1234-ABCDEF",
                               "lvUuid":"FEDCBA-4321-8765-cba9-0fed-4321-FEDCB"+("A" if u==self.data else "B")}
                             for u in self.paths}}
        self.expected={"deviceId":"AAAAAAA-"*7+"AAAAAAA","folderId":"folder",
                       "identitySha256":{n:"a"*64 for n in ("config.xml","cert.pem","key.pem")}}
        self.key,self.public=self.root/"key",self.root/"public"
        subprocess.run(["openssl","genpkey","-algorithm","ED25519","-out",str(self.key)],check=True)
        subprocess.run(["openssl","pkey","-in",str(self.key),"-pubout","-out",str(self.public)],check=True)
        self.anchor={"schema":1,"kind":"generation","tenant":self.cfg["tenant"],"generation":1,
                     "previousDigest":None,"refsDigest":digest(self.manifest),"committedUnix":1}
        self.status={"generation":1,"anchor":sign(self.anchor,self.key),"anchorDigest":digest(self.anchor),
                     "writerFence":{"phase":"open","epoch":1,"writers":{"sync":self.cfg["identity"]}},"repair":None}
        t=self
        class Kube:
            def get(_,path):
                return {"metadata":{"uid":t.data if path.endswith("/sync-data") else t.live}}
        class Witness:
            def call(_,value):
                return copy.deepcopy(t.status)
        class Lvm:
            def inspect(_,cfg,pvc):
                return {"device":"253:1" if pvc==t.data else "253:2"}
        self.kube,self.w,self.lvm=Kube(),Witness(),Lvm()
        self.mountinfo=self.root/"mountinfo"
        self.mountinfo.write_text("")
        for p in (patch.object(rec,"bound_mounts",return_value=self.paths),
                  patch.object(rec,"verify_mount",side_effect=lambda path,state,raw:state)):
            p.start()
            self.addCleanup(p.stop)

    def build(self):
        return rec.build(self.kube,self.w,self.public,self.cfg,self.expected,"node",self.root,
                         self.cfg["machineId"],self.lvm,self.mountinfo)

    def test_current_anchor_and_actual_named_claims(self):
        record=self.build()
        self.assertEqual(record["mounts"],{"data":"253:1","files":"253:2"})
        self.assertEqual(record["refs"],self.manifest)
        self.assertEqual(record["anchor"],self.status["anchor"])
        self.assertEqual(record["identity"],self.cfg["identity"])

    def test_frozen_or_stale_manifest_rejected(self):
        self.status["writerFence"]["phase"]="freezing"
        with self.assertRaises(RecoveryError):
            self.build()
        self.status["writerFence"]["phase"]="open"
        self.anchor["refsDigest"]="0"*64
        self.status["anchor"]=sign(self.anchor,self.key)
        self.status["anchorDigest"]=digest(self.anchor)
        with self.assertRaisesRegex(RecoveryError,"startup_refs_not_current_anchor"):
            self.build()

    def test_record_cannot_replace_existing(self):
        if os.geteuid()!=0:
            self.skipTest("root-only publication exercised separately on host")
        target=self.root/"permit"
        target.mkdir(mode=0o700)
        record=self.build()
        rec.publish(target,record)
        rec.publish(target,record)
        with self.assertRaisesRegex(RecoveryError,"conflicting_startup_record"):
            rec.publish(target,{**record,"epoch":2})
        self.assertEqual(json.loads((target/"startup.json").read_text()),record)
        self.assertEqual((target/"startup.json").stat().st_mode&0o777,0o644)


if __name__=="__main__":
    unittest.main()
