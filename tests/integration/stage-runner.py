import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"server/integrity"))
import stage_runner as runner
import recovery_crypto as c
import git_integrity as g

TENANT="gtm-"+"b"*32


class FakeAPI:
    def __init__(self,ticket,key):
        raw=c.canonical(ticket)
        self.ticket={"ticket":base64.b64encode(raw).decode(),
                     "signature":base64.b64encode(c.sign_bytes(raw,key)).decode(),
                     "activationAllowed":False}
        self.receipt=None
        self.fail_submit=False

    def call(self,value):
        if value["action"]=="status":
            return {"generation":1,"repair":{"operation":"repair-1","targetRole":"sync",
                "targetPvcUid":"pvc-uid","state":"staged_verified" if self.receipt else "ticket_issued"}}
        if value["action"]=="ticket":
            return self.ticket
        if self.fail_submit:
            raise c.RecoveryError("simulated_connection_loss")
        self.receipt=value["evidence"]
        return {"state":"staged_verified","activationAllowed":False}


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.root=root/"stage"
        self.root.mkdir()
        self.repo=root/"repo"
        self.repo.mkdir()
        self.git("init","-qb","main")
        self.git("config","user.name","CI")
        self.git("config","user.email","ci@example.invalid")
        (self.repo/"file").write_bytes(b"ciphertext"*100)
        self.git("add","file")
        self.git("commit","-qm","test")
        for name in ("gtm-tenant","gtm-vault","gtm-mail"):
            self.git("branch",name)
        self.keys={}
        for name in ("witness","target"):
            key,pub=root/(name+".key"),root/(name+".pub")
            subprocess.run(["openssl","genpkey","-algorithm","ED25519","-out",str(key)],check=True)
            subprocess.run(["openssl","pkey","-in",str(key),"-pubout","-out",str(pub)],check=True)
            self.keys[name]=(key,pub)
        now=int(time.time())
        ticket={"schema":1,"operation":"repair-1-1","tenant":TENANT,"generation":1,
                "issuedUnix":now-1,"expiresUnix":now+600,"sourceRole":"history","sourceCloud":"b",
                "sourceUrl":str(self.repo/".git"),"targetRole":"sync","targetCloud":"a",
                "targetPvcUid":"pvc-uid","refs":g.refs(self.repo/".git")}
        self.api=FakeAPI(ticket,self.keys["witness"][0])
        self.identity={"tenant":TENANT,"generation":1,"target_role":"sync",
                       "target_cloud":"a","target_pvc_uid":"pvc-uid"}

    def git(self,*args):
        return subprocess.check_output(["git","-C",str(self.repo),*args],stderr=subprocess.DEVNULL)

    def run_stage(self, **kwargs):
        return runner.run(self.api,self.identity,"repair-1",self.root,self.keys["witness"][1],
                          self.keys["target"][0],60,allow_local_test_source=True,**kwargs)

    def test_rebuild_and_resume_after_lost_receipt(self):
        self.api.fail_submit=True
        try:
            with self.assertRaisesRegex(c.RecoveryError,"simulated_connection_loss"):
                self.run_stage()
        except g.IntegrityError as exc:
            if str(exc)=="direct_io_unsupported":
                self.skipTest("filesystem does not support direct I/O")
            raise
        receipt=self.root/"repair-1-1"/"receipt.json"
        self.assertTrue(receipt.exists())
        self.api.fail_submit=False
        self.assertEqual(self.run_stage()["state"],"staged_verified")
        signed=c.verify(self.api.receipt,self.keys["target"][1])
        self.assertTrue(signed["physicalScrub"])
        self.assertFalse(signed["activationAllowed"])
        self.assertEqual(self.run_stage()["state"],"already_recorded")

    def test_expired_ticket_cannot_start_new_execution(self):
        # Expired signature is valid only for receipt recovery from an already
        # completed staging directory, never for a new fetch.
        raw=base64.b64decode(self.api.ticket["ticket"])
        t=json.loads(raw)
        t["issuedUnix"]=int(time.time())-1200
        t["expiresUnix"]=int(time.time())-600
        self.api=FakeAPI(t,self.keys["witness"][0])
        with self.assertRaisesRegex(c.RecoveryError,"ticket_expired_before_execution"):
            self.run_stage()
        self.assertFalse((self.root/"repair-1-1").exists())

    def test_incomplete_staging_never_overwritten(self):
        stage=self.root/"repair-1-1"
        stage.mkdir()
        (stage/"marker").write_text("keep")
        with self.assertRaisesRegex(c.RecoveryError,"incomplete_staging_requires_new_attempt"):
            self.run_stage()
        self.assertEqual((stage/"marker").read_text(),"keep")

    def test_production_requires_guard_before_any_write(self):
        with self.assertRaisesRegex(c.RecoveryError,"verified_staging_binding_required"):
            runner.run(self.api,self.identity,"repair-1",self.root,self.keys["witness"][1],
                       self.keys["target"][0],60)
        self.assertEqual(list(self.root.iterdir()),[])
        def guard():
            raise c.RecoveryError("wrong_volume")
        with self.assertRaisesRegex(c.RecoveryError,"wrong_volume"):
            self.run_stage(guard=guard)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_guard_detects_root_substitution_before_lock(self):
        previous=self.root.with_name("previous")
        def guard():
            self.root.rename(previous)
            self.root.mkdir()
            return {"mount":str(self.root),"device":"0:0"}
        with patch.object(runner,"validate_mount"):
            with self.assertRaisesRegex(c.RecoveryError,"staging_mount_changed"):
                self.run_stage(guard=guard)
        self.assertEqual(list(self.root.iterdir()),[])
        self.assertEqual(list(previous.iterdir()),[])

    def test_guard_rechecked_before_receipt_and_retry(self):
        calls=[]
        def guard():
            calls.append(1)
            if len(calls)==3:
                raise c.RecoveryError("target_no_longer_healthy")
            return {"mount":str(self.root),"device":"0:0"}
        with patch.object(runner,"validate_mount"):
            try:
                with self.assertRaisesRegex(c.RecoveryError,"target_no_longer_healthy"):
                    self.run_stage(guard=guard)
            except g.IntegrityError as exc:
                if str(exc)=="direct_io_unsupported":
                    self.skipTest("filesystem does not support direct I/O")
                raise
            self.assertEqual(len(calls),3)
            self.assertIsNone(self.api.receipt)
            self.assertTrue((self.root/"repair-1-1"/"receipt.json").is_file())
            self.assertEqual(self.run_stage(guard=guard)["state"],"staged_verified")

    def test_unrelated_data_preserved_and_rejected(self):
        marker=self.root/"unrelated"
        marker.write_bytes(b"keep")
        with self.assertRaisesRegex(c.RecoveryError,"unexpected_staging_content"):
            self.run_stage()
        self.assertEqual(marker.read_bytes(),b"keep")
        self.assertIsNone(self.api.receipt)

    def test_recorded_sync_requires_matching_witness_receipt(self):
        try:
            self.run_stage()
        except g.IntegrityError as exc:
            if str(exc)=="direct_io_unsupported":
                self.skipTest("filesystem does not support direct I/O")
            raise
        ticket=json.loads(base64.b64decode(self.api.ticket["ticket"]))
        receipt=c.verify(self.api.receipt,self.keys["target"][1])
        original=self.api.call
        def call(value):
            if value["action"]=="status":
                result=original(value)
                result["writerFence"]={"phase":"frozen"}
                result["repair"].update(ticket=ticket,receipt=receipt)
                return result
            return original(value)
        self.api.call=call
        cfg={"deviceId":"unused-in-mock"}
        with patch("sync_state.prepare",return_value={"state":"sync_state_prepared","activationAllowed":False}) as prepare:
            self.assertEqual(self.run_stage(sync_config=cfg,sync_age_key="test-only")["state"],"sync_state_prepared")
            self.assertEqual(prepare.call_count,1)
            self.assertEqual(prepare.call_args.args[2],ticket["refs"])
            receipt["ticketSha256"]="0"*64
            with self.assertRaisesRegex(c.RecoveryError,"sync_state_receipt_mismatch"):
                self.run_stage(sync_config=cfg,sync_age_key="test-only")
            self.assertEqual(prepare.call_count,1)

    def test_mismatch_and_unverified_mount(self):
        self.identity["target_pvc_uid"]="wrong"
        with self.assertRaises(c.RecoveryError):
            self.run_stage()
        with self.assertRaises(c.RecoveryError):
            runner.validate_mount(self.root,"0:0")


if __name__=="__main__":
    unittest.main()
