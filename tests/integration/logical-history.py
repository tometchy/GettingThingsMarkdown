import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("logical_history",
    Path(__file__).resolve().parents[2] / "server/sync/logical_history.py")
meter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(meter)

class LogicalHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.invalid")
    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.root), *args]).strip()
    def commit(self, path, data):
        (self.root / path).write_bytes(data)
        self.git("add", "-A")
        self.git("commit", "-qm", "test")
    def usage(self):
        return meter.measure(self.root / ".git")
    def test_unique_history_deleted_versions_and_other_branches(self):
        self.commit("one", b"abc")
        self.commit("two", b"abc")
        self.commit("one", b"new-data")
        (self.root / "two").unlink()
        self.git("add", "-A")
        self.git("commit", "-qm", "delete")
        self.assertEqual(self.usage()["logicalHistoryBytes"], 11)
        self.assertEqual(self.usage()["blobCount"], 2)
        self.git("switch", "-qc", "gtm-mail")
        self.commit("mail", b"not charged")
        self.assertEqual(self.usage()["logicalHistoryBytes"], 11)
        self.git("gc", "--prune=now")
        self.assertEqual(self.usage()["logicalHistoryBytes"], 11)
    def test_protected_ref_is_charged_once_until_removed(self):
        self.commit("a", b"abc")
        base = self.git("rev-parse", "HEAD").decode()
        self.commit("a", b"12345")
        self.git("update-ref", "refs/gtmd/protected/request-1", "HEAD")
        self.git("reset", "--hard", base)
        self.assertEqual(self.usage()["logicalHistoryBytes"], 8)
        self.git("update-ref", "-d", "refs/gtmd/protected/request-1")
        self.assertEqual(self.usage()["logicalHistoryBytes"], 3)
    def test_missing_object_fails_closed(self):
        self.commit("a", b"abc")
        oid = self.git("rev-parse", "HEAD:a").decode()
        (self.root / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            self.usage()

if __name__ == "__main__":
    unittest.main()
