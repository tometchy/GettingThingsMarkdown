import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server/integrity"))
import git_integrity as g
import worktree_integrity as w


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "git"
        self.work = root / "files"
        self.work.mkdir()
        self.git("init", "-qb", "main")
        self.git("config", "user.name", "CI")
        self.git("config", "user.email", "ci@example.invalid")
        (self.work / "data").write_bytes(b"ciphertext")
        (self.work / "directory").mkdir()
        (self.work / "directory" / "tab\tnewline\n").write_bytes(b"more")
        with open(os.fsencode(self.work) + b"/nonutf8-\xff", "wb") as f:
            f.write(b"opaque")
        self.git("add", "-A")
        self.git("commit", "-qm", "initial")
        self.main = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.check_output(["git", "--git-dir", str(self.repo),
                                        "--work-tree", str(self.work), *args],
                                       stderr=subprocess.DEVNULL).decode().strip()

    def verify(self):
        return w.verify(self.repo, self.work, self.main, 60)

    def test_all_raw_path_bytes(self):
        result = self.verify()
        self.assertEqual(result["files"], 3)
        self.assertEqual(result["verifiedBytes"], 20)

    def test_same_size_silent_corruption(self):
        (self.work / "data").write_bytes(b"Ciphertext")
        with self.assertRaisesRegex(g.IntegrityError, "worktree_blob_mismatch"):
            self.verify()

    def test_missing_and_extra(self):
        (self.work / "data").unlink()
        with self.assertRaisesRegex(g.IntegrityError, "missing_worktree_file"):
            self.verify()
        (self.work / "data").write_bytes(b"ciphertext")
        (self.work / ".stignore").write_bytes(b"new unverified content")
        with self.assertRaisesRegex(g.IntegrityError, "unexpected_worktree_file"):
            self.verify()

    def test_symlink_and_stale_main(self):
        (self.work / "data").unlink()
        (self.work / "data").symlink_to("/dev/null")
        with self.assertRaises(g.IntegrityError):
            self.verify()
        (self.work / "data").unlink()
        (self.work / "data").write_bytes(b"new")
        self.git("add", "data")
        self.git("commit", "-qm", "new")
        with self.assertRaisesRegex(g.IntegrityError, "worktree_manifest_mismatch"):
            self.verify()


if __name__ == "__main__":
    unittest.main()
