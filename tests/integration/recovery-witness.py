import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server/integrity"))
import recovery_crypto as crypto
import recovery_witness as w
import replica_evidence as e
import git_integrity as g

TENANT = "gtm-" + "a"*32


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 10000
        self.keys = {}
        self.public = {}
        for role in ("witness", "sync", "history"):
            key, pub = self.root / (role+".key"), self.root / (role+".pub")
            subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)], check=True)
            subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(pub)], check=True)
            self.keys[role], self.public[role] = key, pub
        raw = {"tenants": {TENANT: {role: {"cloud": role+"-cloud", "publicKey": str(self.public[role]),
                "sourceUrl": "https://"+role+".example.invalid/repo.git"} for role in ("sync","history")}}}
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps(raw))
        self.cfg = w.config(self.config, self.keys["witness"])
        self.state = self.root / "state"
        self.w = w.Witness(self.state, self.cfg, self.keys["witness"], lambda:self.now)
        self.w.initialize(TENANT)
        self.repo = self.root / "work"
        self.repo.mkdir()
        self.git("init","-qb","main")
        self.git("config","user.name","CI")
        self.git("config","user.email","ci@example.invalid")
        (self.repo/"file").write_bytes(b"encrypted")
        self.git("add","file")
        self.git("commit","-qm","first")
        for b in ("gtm-tenant","gtm-vault","gtm-mail"):
            self.git("branch",b)
        self.git("update-ref","refs/gtmd/protected/hold","HEAD")

    def git(self,*args):
        return subprocess.check_output(["git","-C",str(self.repo),*args],stderr=subprocess.DEVNULL).decode().strip()

    def attest(self,ch,role):
        return e.attest(self.repo/".git", ch, self.public["witness"], self.keys[role],
                        TENANT,role,role+"-cloud",lambda:self.now,60)

    def anchor(self):
        ch=self.w.challenge(TENANT,"anchor")
        a,b=self.attest(ch,"sync"),self.attest(ch,"history")
        return self.w.anchor(TENANT,a,b)

    def start(self):
        self.anchor()
        ch=self.w.challenge(TENANT,"repair","sync","new-pvc")
        repair=self.w.start_repair(TENANT,self.attest(ch,"history"))
        return repair["operation"]

    def test_monotonic_anchor_restart_and_replay(self):
        ch=self.w.challenge(TENANT,"anchor")
        a,b=self.attest(ch,"sync"),self.attest(ch,"history")
        anchor=self.w.anchor(TENANT,a,b)
        self.assertEqual(crypto.verify(anchor,self.public["witness"])["generation"],1)
        with self.assertRaises(crypto.RecoveryError):
            self.w.anchor(TENANT,a,b)
        self.w=w.Witness(self.state,self.cfg,self.keys["witness"],lambda:self.now)
        (self.repo/"file").write_bytes(b"changed")
        self.git("add","file")
        self.git("commit","-qm","next")
        second=crypto.verify(self.anchor(),self.public["witness"])
        self.assertEqual(second["generation"],2)
        self.assertEqual(second["previousDigest"],crypto.digest(anchor["payload"]))

    def test_wrong_key_and_disagreement(self):
        ch=self.w.challenge(TENANT,"anchor")
        a=self.attest(ch,"sync")
        with self.assertRaises(crypto.RecoveryError):
            self.w.anchor(TENANT,a,a)
        b=self.attest(ch,"history")
        b["payload"]["refs"]["refs/heads/main"]="1"*40
        b=crypto.sign(b["payload"],self.keys["history"])
        with self.assertRaisesRegex(crypto.RecoveryError,"replicas_disagree"):
            self.w.anchor(TENANT,a,b)
        self.assertEqual(self.w.status(TENANT)["generation"],0)

    def test_rollback_or_deleted_ref(self):
        self.anchor()
        (self.repo/"file").write_bytes(b"next")
        self.git("add","file")
        self.git("commit","-qm","next")
        self.anchor()
        self.git("reset","--hard","HEAD~1")
        ch=self.w.challenge(TENANT,"anchor")
        with self.assertRaisesRegex(crypto.RecoveryError,"non_fast_forward"):
            self.attest(ch,"sync")

    def test_repair_ticket_receipt_and_no_activation(self):
        op=self.start()
        raw,sig=self.w.ticket(TENANT,op)
        crypto.verify_bytes(raw,sig,self.public["witness"])
        self.assertEqual((raw,sig),self.w.ticket(TENANT,op))
        t=json.loads(raw)
        self.assertEqual(t["generation"],1)
        self.assertEqual(t["targetPvcUid"],"new-pvc")
        self.assertEqual(t["sourceRole"],"history")
        with self.assertRaises(crypto.RecoveryError):
            self.w.challenge(TENANT,"anchor")
        receipt={"schema":1,"operation":t["operation"],"tenant":TENANT,"generation":1,
                 "targetPvcUid":"new-pvc","ticketSha256":hashlib.sha256(raw).hexdigest(),
                 "refsVerified":len(t["refs"]),"activationAllowed":False,"state":"staged_verified",
                 "physicalScrub":False,"completedUnix":self.now}
        with self.assertRaises(crypto.RecoveryError):
            self.w.receipt(TENANT,op,crypto.sign(receipt,self.keys["sync"]))
        receipt["physicalScrub"]=True
        with self.assertRaises(crypto.RecoveryError):
            self.w.receipt(TENANT,op,crypto.sign(receipt,self.keys["history"]))
        result=self.w.receipt(TENANT,op,crypto.sign(receipt,self.keys["sync"]))
        self.assertFalse(result["activationAllowed"])
        self.assertEqual(self.w.status(TENANT)["repair"]["state"],"staged_verified")
        self.assertEqual(result,self.w.receipt(TENANT,op,crypto.sign(receipt,self.keys["sync"])))

    def test_retry_after_expiry_and_abort(self):
        op=self.start()
        first=json.loads(self.w.ticket(TENANT,op)[0])
        with self.assertRaises(crypto.RecoveryError):
            self.w.abort(TENANT,op)
        self.now+=w.TICKET_TTL
        self.assertEqual(first,json.loads(self.w.ticket(TENANT,op)[0]))
        with self.assertRaises(crypto.RecoveryError):
            self.w.abort(TENANT,op)
        def retire(ticket):
            evidence={"schema":1,"kind":"attempt_retired","operation":ticket["operation"],
                "tenant":TENANT,"generation":1,"targetPvcUid":"new-pvc",
                "ticketSha256":hashlib.sha256(crypto.canonical(ticket)).hexdigest(),
                "stagingStopped":True,"stagingIsolated":True,"verifiedUnix":self.now}
            with self.assertRaises(crypto.RecoveryError):
                self.w.retire_attempt(TENANT,op,crypto.sign(evidence,self.keys["history"]))
            self.w.retire_attempt(TENANT,op,crypto.sign(evidence,self.keys["sync"]))
        retire(first)
        second=json.loads(self.w.ticket(TENANT,op)[0])
        self.assertNotEqual(first["operation"],second["operation"])
        self.now+=w.TICKET_TTL
        retire(second)
        self.assertEqual(self.w.abort(TENANT,op)["state"],"aborted")
        self.assertIsNone(self.w.status(TENANT)["repair"])

    def test_concurrent_challenge_cas(self):
        def call(_):
            try:
                self.w.challenge(TENANT,"anchor")
                return True
            except crypto.RecoveryError:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(call,range(4))),1)

    def test_stale_challenge_and_distinct_keys(self):
        ch=self.w.challenge(TENANT,"anchor")
        self.now+=w.TTL
        with self.assertRaises(crypto.RecoveryError):
            self.attest(ch,"sync")
        cfg=json.loads(self.config.read_text())
        cfg["tenants"][TENANT]["history"]["publicKey"]=str(self.public["sync"])
        self.config.write_text(json.dumps(cfg))
        with self.assertRaises(crypto.RecoveryError):
            w.config(self.config,self.keys["witness"])

    def test_signed_state_detects_corruption_and_trust_change(self):
        self.anchor()
        path = self.state / (TENANT+".json")
        raw = path.read_bytes()
        envelope = json.loads(raw)
        envelope["payload"]["generation"] = 99
        path.write_text(json.dumps(envelope))
        with self.assertRaises(crypto.RecoveryError):
            self.w.status(TENANT)
        path.write_bytes(raw)
        changed = json.loads(self.config.read_text())
        changed["tenants"][TENANT]["sync"]["sourceUrl"] = "https://different.example.invalid/repo.git"
        self.config.write_text(json.dumps(changed))
        other = w.Witness(self.state, w.config(self.config,self.keys["witness"]),
                          self.keys["witness"], lambda:self.now)
        with self.assertRaisesRegex(crypto.RecoveryError,"invalid_witness_state_or_trust_change"):
            other.status(TENANT)

    def test_stale_evidence_even_before_challenge_expiry(self):
        ch = self.w.challenge(TENANT,"anchor")
        a,b = self.attest(ch,"sync"),self.attest(ch,"history")
        self.now += w.EVIDENCE_MAX_AGE+1
        with self.assertRaisesRegex(crypto.RecoveryError,"evidence_time_invalid"):
            self.w.anchor(TENANT,a,b)

    def test_missing_state_is_not_valid_anchor(self):
        (self.state / (TENANT+".json")).unlink()
        with self.assertRaisesRegex(crypto.RecoveryError,"witness_state_missing"):
            self.w.status(TENANT)
        with self.assertRaisesRegex(crypto.RecoveryError,"witness_state_missing"):
            self.w.challenge(TENANT,"anchor")


if __name__=="__main__":
    unittest.main()
