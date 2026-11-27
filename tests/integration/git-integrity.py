import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

spec = importlib.util.spec_from_file_location("git_integrity",
    Path(__file__).resolve().parents[2] / "server/integrity/git_integrity.py")
integrity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(integrity)


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.repo = self.work / ".git"
        self.git("init", "-qb", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.commit("old", b"old ciphertext")
        self.old = self.git("rev-parse", "HEAD:old")
        self.commit("old", b"new ciphertext")
        self.git("tag", "keep")
        self.git("update-ref", "refs/gtmd/protected/hold", "HEAD~1")
        for branch in ("gtm-tenant", "gtm-vault", "gtm-mail"):
            self.git("switch", "-qc", branch)
            self.commit(branch, branch.encode())
            self.git("switch", "-q", "main")
        self.expected = integrity.refs(self.repo)

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.work), *args],
                                       stderr=subprocess.DEVNULL).decode().strip()

    def commit(self, name, data):
        (self.work / name).write_bytes(data)
        self.git("add", name)
        self.git("commit", "-qm", "test")

    def loose(self, oid):
        return self.repo / "objects" / oid[:2] / oid[2:]

    def flip(self, path):
        data = bytearray(path.read_bytes())
        data[len(data)//2] ^= 1
        # Git objects are normally read-only; simulate physical corruption
        # as the owner of a disposable test repository, not as root.
        path.chmod(path.stat().st_mode | 0o200)
        path.write_bytes(data)

    def test_all_refs_and_rebuild(self):
        self.assertEqual(integrity.scan(self.repo, self.expected)["refs"], 6)
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps(self.expected))
        staged = self.root / "staged.git"
        integrity.stage_rebuild(str(self.repo), staged, integrity.load_manifest(manifest))
        self.assertEqual(integrity.refs(staged), self.expected)
        self.assertFalse((staged / "objects/info/alternates").exists())
        self.assertEqual(integrity.git(staged, "cat-file", "blob", self.old, capture=True),
                         "old ciphertext")
        with self.assertRaises(FileExistsError):
            integrity.stage_rebuild(str(self.repo), staged, self.expected)
        self.assertEqual(integrity.refs(self.repo), self.expected)

    def test_old_loose_blob_corruption_and_repair(self):
        good = self.root / "good.git"
        integrity.stage_rebuild(str(self.repo), good, self.expected)
        self.flip(self.loose(self.old))
        with self.assertRaises(integrity.IntegrityError):
            integrity.scan(self.repo)
        status = self.root / "status.json"
        state = integrity.cycle(self.repo, status, "sync", 60)
        self.assertEqual(state["state"], "degraded")
        self.assertEqual(json.loads(status.read_text())["severity"], "critical")
        restored = self.root / "restored.git"
        integrity.stage_rebuild(str(good), restored, self.expected)
        integrity.scan(restored, self.expected)
        with self.assertRaises(integrity.IntegrityError):
            integrity.scan(self.repo)

    def test_missing_object_in_non_main_branch(self):
        oid = self.git("rev-parse", "gtm-vault:gtm-vault")
        self.loose(oid).unlink()
        with self.assertRaises(integrity.IntegrityError):
            integrity.scan(self.repo)

    def test_pack_and_index_corruption(self):
        self.git("gc", "--prune=now")
        integrity.scan(self.repo)
        pack = next((self.repo / "objects/pack").glob("*.pack"))
        self.flip(pack)
        with self.assertRaises(integrity.IntegrityError):
            integrity.scan(self.repo)

    def test_corrupt_index(self):
        self.git("gc", "--prune=now")
        self.flip(next((self.repo / "objects/pack").glob("*.idx")))
        with self.assertRaises(integrity.IntegrityError):
            integrity.scan(self.repo)

    def test_watchdog_status_age_and_stalled_scan(self):
        status = self.root / "status.json"
        now = 200000
        self.assertEqual(integrity.status_health(status, now)[0], "critical")
        integrity.atomic_json(status, {"state": "verified", "lastSuccessfulUnix": now - 60,
                                      "updatedUnix": now - 60})
        self.assertEqual(integrity.status_health(status, now)[0], "info")
        integrity.atomic_json(status, {"state": "scanning", "lastSuccessfulUnix": now - 90000,
                                      "updatedUnix": now - 100})
        self.assertEqual(integrity.status_health(status, now)[0], "warning")
        integrity.atomic_json(status, {"state": "scanning", "lastSuccessfulUnix": now - 100,
                                      "updatedUnix": now - 90000})
        self.assertEqual(integrity.status_health(status, now)[0], "critical")
        integrity.atomic_json(status, {"state": "verified", "lastSuccessfulUnix": now - 180000,
                                      "updatedUnix": now - 60})
        self.assertEqual(integrity.status_health(status, now)[0], "critical")
        integrity.atomic_json(status, {"state": "verified", "lastSuccessfulUnix": now - 60,
                                      "updatedUnix": now - 1000})
        self.assertEqual(integrity.status_health(status, now)[0], "critical")

    def test_missing_index(self):
        self.git("gc", "--prune=now")
        next((self.repo / "objects/pack").glob("*.idx")).unlink()
        with self.assertRaises(integrity.IntegrityError):
            integrity.scan(self.repo)

    def test_valid_but_stale_repo_not_accepted(self):
        self.git("update-ref", "-d", "refs/heads/gtm-mail")
        integrity.scan(self.repo)
        with self.assertRaises(integrity.IntegrityError):
            integrity.scan(self.repo, self.expected)
        with self.assertRaises(integrity.IntegrityError):
            integrity.stage_rebuild(str(self.repo), self.root / "stale.git", self.expected)

    def test_unreachable_corrupt_object(self):
        oid = subprocess.check_output(["git", "-C", str(self.work), "hash-object",
                                       "-w", "--stdin"], input=b"unreachable").decode().strip()
        self.flip(self.loose(oid))
        with self.assertRaises(integrity.IntegrityError):
            integrity.scan(self.repo)

    def test_alternate_objects_not_accepted(self):
        p = self.repo / "objects/info/alternates"
        p.write_text(str(self.repo / "objects"))
        with self.assertRaises(integrity.IntegrityError):
            integrity.scan(self.repo)

    def test_private_metrics(self):
        status = self.root / "status.json"
        self.assertIn('severity="critical"} 1', integrity.metrics_text(status))
        now = time.time()
        integrity.atomic_json(status, {"state": "verified", "lastSuccessfulUnix": now - 60,
                                      "updatedUnix": now - 60, "secret": "DO_NOT_EXPORT"})
        text = integrity.metrics_text(status)
        self.assertIn('severity="info"} 1', text)
        self.assertNotIn("DO_NOT_EXPORT", text)
        integrity.atomic_json(status, {"state": "scanning", "lastSuccessfulUnix": now - 90000,
                                      "updatedUnix": now - 100})
        self.assertIn('severity="warning"} 1', integrity.metrics_text(status))

    def test_good_status_does_not_leak_refs(self):
        status = self.root / "status.json"
        state = integrity.cycle(self.repo, status, "history", 60)
        self.assertEqual(state["state"], "verified")
        self.assertNotIn("gtm-vault", status.read_text())
        self.assertNotIn(str(self.repo), status.read_text())


if __name__ == "__main__":
    unittest.main()
