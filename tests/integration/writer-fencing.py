import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"server/integrity"))
import writer_fence as f
import writer_guard as guard
import writer_permit_agent as agent
import recovery_orchestrator as orchestration
import witness_api as api
import recovery_crypto as c
import recovery_witness as w

TENANT="gtm-"+"c"*32
IDS={role:{"podUid":role+"-pod","pvcUids":[role+"-pvc"],"nodeUid":role+"-node"}
     for role in ("sync","history")}
IDS["sync"]["pvcUids"]=["sync-live-pvc","sync-pvc"]


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.keys={}
        self.now=10000
        for role in ("witness","sync","history"):
            key,pub=self.root/(role+".key"),self.root/(role+".pub")
            subprocess.run(["openssl","genpkey","-algorithm","ED25519","-out",str(key)],check=True)
            subprocess.run(["openssl","pkey","-in",str(key),"-pubout","-out",str(pub)],check=True)
            self.keys[role]=(key,pub)
        p=self.root/"config.json"
        p.write_text(json.dumps({"tenants":{TENANT:{role:{"cloud":role,
          "publicKey":str(self.keys[role][1]),"sourceUrl":"https://"+role+".example.invalid/git"}
          for role in ("sync","history")}}}))
        self.w=w.Witness(self.root/"state",w.config(p,self.keys["witness"][0]),
                         self.keys["witness"][0],lambda:self.now)
        self.w.initialize(TENANT)
        f.enroll(self.w,TENANT,IDS)

    def request(self,role="sync",rid="first",**extra):
        data={"schema":1,"kind":"writer_permit_request","tenant":TENANT,"role":role,
              "epoch":1,"identity":IDS[role],"requestId":rid,"issuedUnix":self.now}
        data.update(extra)
        return c.sign(data,self.keys[role][0])

    def test_lease_replay_identity_and_freeze(self):
        req=self.request()
        first=f.permit(self.w,TENANT,"sync",req)
        self.assertEqual(first,f.permit(self.w,TENANT,"sync",req))
        with self.assertRaises(c.RecoveryError):
            f.permit(self.w,TENANT,"history",req)
        with self.assertRaises(c.RecoveryError):
            f.permit(self.w,TENANT,"sync",self.request("sync","wrong",identity=IDS["history"]))
        self.now+=1
        second=f.permit(self.w,TENANT,"sync",self.request("sync","second"))
        self.assertEqual(second["payload"]["sequence"],2)
        with self.assertRaises(c.RecoveryError):
            f.permit(self.w,TENANT,"sync",req)
        # Emergency freeze supersedes in-progress anchor evidence.
        self.w.challenge(TENANT,"anchor")
        ch=f.freeze(self.w,TENANT,"incident-1")
        self.assertIsNone(self.w.status(TENANT)["challenge"])
        self.assertEqual(ch,f.freeze(self.w,TENANT,"incident-1"))
        with self.assertRaises(c.RecoveryError):
            f.permit(self.w,TENANT,"sync",self.request("sync","third"))
        with self.assertRaises(c.RecoveryError):
            self.w.challenge(TENANT,"repair","sync","new-pvc")

    def stop(self,ch,role,**extra):
        data={"schema":1,"kind":"writer_stopped","tenant":TENANT,"role":role,
            "challengeDigest":c.digest(ch["payload"]),"identity":IDS[role],
            "epoch":ch["payload"]["epoch"],"processesStopped":True,
            "writeAccessRevoked":True,"oldVolumesIsolated":True,"method":"trusted_host_runtime",
            "evidenceDigest":"a"*64,"verifiedUnix":self.now}
        data.update(extra)
        return c.sign(data,self.keys[role][0])

    def test_both_stop_proofs_required_after_deadline(self):
        f.permit(self.w,TENANT,"sync",self.request())
        ch=f.freeze(self.w,TENANT,"incident-1")
        with self.assertRaises(c.RecoveryError):
            f.confirm_stop(self.w,TENANT,"sync",self.stop(ch,"sync"))
        self.now=ch["payload"]["notBeforeUnix"]
        with self.assertRaises(c.RecoveryError):
            f.confirm_stop(self.w,TENANT,"sync",self.stop(ch,"sync",oldVolumesIsolated=False))
        proof=self.stop(ch,"sync")
        self.assertEqual(f.confirm_stop(self.w,TENANT,"sync",proof)["phase"],"freezing")
        self.assertEqual(f.confirm_stop(self.w,TENANT,"history",self.stop(ch,"history"))["phase"],"frozen")
        self.assertFalse(f.cutover_status(self.w.status(TENANT))["activationAllowed"])
        with self.assertRaises(c.RecoveryError):
            self.w.challenge(TENANT,"anchor")

    def test_guard_monotonic_expiry_and_epoch(self):
        first=f.permit(self.w,TENANT,"sync",self.request())
        wall=[self.now]
        mono=[50]
        g=guard.Guard(TENANT,"sync",IDS["sync"],self.keys["witness"][1],
                      lambda:wall[0],lambda:mono[0])
        g.check(first)
        self.assertEqual(g.deadline,105)
        wall[0]-=5
        mono[0]=104
        g.check(first)
        self.assertEqual(g.deadline,105)
        mono[0]=105
        with self.assertRaises(c.RecoveryError):
            g.check(first)
        other=guard.Guard(TENANT,"history",IDS["history"],self.keys["witness"][1],
                          lambda:self.now,lambda:50)
        with self.assertRaises(c.RecoveryError):
            other.check(first)

    def test_guard_stops_real_process_group(self):
        now=int(time.time())
        payload={"schema":1,"kind":"writer_permit","tenant":TENANT,"role":"sync",
                 "epoch":1,"identity":IDS["sync"],"sequence":1,
                 "issuedUnix":now,"expiresUnix":now+7}
        permit=self.root/"permit.json"
        permit.write_text(json.dumps(c.sign(payload,self.keys["witness"][0])))
        marker=self.root/"writes"
        script="import time,sys\nwith open(sys.argv[1],'a',buffering=1) as f:\n while True:\n  f.write('write\\n');time.sleep(.03)"
        g=guard.Guard(TENANT,"sync",IDS["sync"],self.keys["witness"][1])
        rc=guard.supervise(g,permit,[sys.executable,"-c",script,str(marker)],
                           poll=.1,require_pid1=False)
        self.assertEqual(rc,1)
        self.assertTrue(marker.exists())
        size=marker.stat().st_size
        time.sleep(.15)
        self.assertEqual(marker.stat().st_size,size)

    def test_host_publisher_atomic_readable(self):
        class API:
            def call(_,v):
                return f.permit(self.w,TENANT,v["role"],v["evidence"])
        out=self.root/"permit.json"
        result=agent.renew(API(),TENANT,"sync",IDS["sync"],1,self.keys["sync"][0],
                           self.keys["witness"][1],out,self.now)
        self.assertEqual(result["state"],"permit_published")
        self.assertEqual(out.stat().st_mode&0o777,0o644)
        self.assertEqual(c.verify(json.loads(out.read_text()),self.keys["witness"][1])["identity"],IDS["sync"])

    def test_orchestrator_persists_target_and_resumes(self):
        # Reference state-machine test: real signatures, synthetic refs;
        # full Git attestation and HTTPS fetch have separate real-Git tests.
        ch=self.w.challenge(TENANT,"anchor")
        refs={r:"1"*40 for r in w.REQUIRED}
        def evidence(ch,role):
            return c.sign({"schema":1,"kind":"replica_scan","tenant":TENANT,"role":role,
                "cloud":role,"challengeDigest":c.digest(ch["payload"]),"refs":refs,
                "verifiedUnix":self.now,"appendOnlyVerified":True},self.keys[role][0])
        self.w.anchor(TENANT,evidence(ch,"sync"),evidence(ch,"history"))
        class API:
            def call(_,value):
                return api.dispatch(self.w,value)
        client=API()
        def run(**kwargs):
            return orchestration.reconcile(client,TENANT,"incident-2","sync","new-pvc",**kwargs)
        pending=run()
        self.assertEqual(pending["state"],"awaiting_writer_stop")
        self.assertEqual(c.verify(pending["freezeChallenge"],self.keys["witness"][1]),
                         self.w.status(TENANT)["writerFence"]["freeze"]["challenge"])
        self.assertEqual(run()["freezeChallenge"],pending["freezeChallenge"])
        with self.assertRaises(c.RecoveryError):
            orchestration.reconcile(client,TENANT,"incident-2","sync","other-pvc")
        freeze=self.w.status(TENANT)["writerFence"]["freeze"]["challenge"]
        self.now=freeze["notBeforeUnix"]
        ch={"payload":freeze}
        result=run(stop_proofs={role:self.stop(ch,role) for role in ("sync","history")})
        self.assertEqual(result["state"],"awaiting_source_evidence")
        self.assertEqual(run()["challenge"],result["challenge"])
        result=run(evidence=evidence(result["challenge"],"history"))
        self.assertEqual(result["state"],"awaiting_staging_worker")
        self.assertEqual(result,run())
        self.assertFalse(result["activationAllowed"])

    def test_freeze_concurrent_with_renewal_never_reopens(self):
        req=self.request()
        def call():
            try:
                f.permit(self.w,TENANT,"sync",req)
            except c.RecoveryError:
                pass
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(call)
            b=pool.submit(f.freeze,self.w,TENANT,"incident-1")
            a.result()
            ch=b.result()
        state=self.w.status(TENANT)["writerFence"]
        self.assertEqual(state["phase"],"freezing")
        for lease in state["leases"].values():
            self.assertGreaterEqual(ch["payload"]["notBeforeUnix"],lease["expiresUnix"]+f.CLOCK_SKEW)
        with self.assertRaises(c.RecoveryError):
            f.permit(self.w,TENANT,"sync",req)


if __name__=="__main__":
    unittest.main()
