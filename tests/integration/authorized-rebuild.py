import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server/integrity"))
import authorized_rebuild as a
import git_integrity as g
import physical_scrub as s

TENANT = "gtm-" + "a" * 32


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.repo = self.work / ".git"
        self.git("init", "-qb", "main")
        self.git("config", "user.name", "CI")
        self.git("config", "user.email", "ci@example.invalid")
        (self.work / "blob").write_bytes(b"opaque ciphertext" * 100)
        self.git("add", "blob")
        self.git("commit", "-qm", "first")
        for branch in ("gtm-tenant", "gtm-vault", "gtm-mail"):
            self.git("branch", branch)
        self.git("tag", "tag")
        self.git("update-ref", "refs/gtmd/protected/hold", "HEAD")
        self.key = self.root / "private"
        self.pub = self.root / "public"
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(self.key)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["openssl", "pkey", "-in", str(self.key), "-pubout", "-out", str(self.pub)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.ticket = self.root / "ticket.json"
        self.sig = self.root / "ticket.sig"
        self.now = int(time.time())
        self.data = {"schema": 1, "operation": "repair-1", "tenant": TENANT, "generation": 5,
            "issuedUnix": self.now-1, "expiresUnix": self.now+300,
            "sourceRole": "history", "sourceCloud": "b", "sourceUrl": str(self.repo),
            "targetRole": "sync", "targetCloud": "a", "targetPvcUid": "new-pvc-uid",
            "refs": g.refs(self.repo)}

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.work), *args],
                                       stderr=subprocess.DEVNULL).decode().strip()

    def sign(self):
        self.ticket.write_text(json.dumps(self.data))
        subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(self.key), "-rawin",
                        "-in", str(self.ticket), "-out", str(self.sig)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def verify(self, **kw):
        args = dict(tenant=TENANT, generation=5, target_role="sync", target_cloud="a",
                    target_pvc_uid="new-pvc-uid", now=self.now, allow_local_test_source=True)
        args.update(kw)
        return a.verify_ticket(self.ticket, self.sig, self.pub, **args)

    def test_signed_rebuild_and_no_cutover(self):
        self.sign()
        ticket, digest = self.verify()
        dest = self.root / "staging"
        receipt = a.execute(ticket, digest, dest, 60)
        self.assertFalse(receipt["activationAllowed"])
        self.assertEqual(g.refs(dest / "repository.git"), self.data["refs"])
        self.assertEqual(json.loads((dest / "receipt.json").read_text()), receipt)
        with self.assertRaises(FileExistsError):
            a.execute(ticket, digest, dest, 60)

    def test_modified_signature_or_ticket(self):
        self.sign()
        self.ticket.write_text(self.ticket.read_text() + " ")
        with self.assertRaises(g.IntegrityError):
            self.verify()

    def test_target_generation_expiry_and_source(self):
        self.sign()
        for kw in (dict(generation=6), dict(target_pvc_uid="other"), dict(tenant="other"),
                   dict(now=self.now+301), dict(allow_local_test_source=False)):
            with self.assertRaises(g.IntegrityError):
                self.verify(**kw)
        self.data["sourceCloud"] = "a"
        self.sign()
        with self.assertRaises(g.IntegrityError):
            self.verify()

    def test_missing_branch_and_valid_but_stale_source(self):
        original = dict(self.data["refs"])
        del self.data["refs"]["refs/heads/gtm-vault"]
        self.sign()
        with self.assertRaises(g.IntegrityError):
            self.verify()
        self.data["refs"] = original
        self.sign()
        ticket, digest = self.verify()
        self.git("branch", "-D", "gtm-vault")
        with self.assertRaises(g.IntegrityError):
            a.execute(ticket, digest, self.root / "bad", 60)
        self.assertFalse((self.root / "bad/receipt.json").exists())

    def test_physical_direct_read(self):
        try:
            result = s.scrub(self.repo, 60)
        except g.IntegrityError as exc:
            if str(exc) == "direct_io_unsupported":
                self.skipTest("test filesystem does not support direct I/O")
            raise
        self.assertGreater(result["directReadBytes"], 0)
        self.assertFalse(result["hardwareCacheBypassGuaranteed"])

    def test_physical_read_mismatch_never_verified(self):
        with patch.object(s, "read_digest", side_effect=lambda p, i, direct, d:
                          b"a" if direct else b"b"):
            with self.assertRaisesRegex(g.IntegrityError, "direct_buffered_digest_mismatch"):
                s.scrub(self.repo, 60)

    def test_physical_symlink_refused(self):
        (self.repo / "objects/extra").symlink_to(self.root / "private")
        with self.assertRaises(g.IntegrityError):
            s.scrub(self.repo, 60)


if __name__ == "__main__":
    unittest.main()
