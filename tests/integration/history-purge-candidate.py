import importlib.util, pathlib, subprocess, tempfile, unittest
spec = importlib.util.spec_from_file_location("purge", pathlib.Path(__file__).resolve().parents[2] / "scripts/recovery/prepare-history-purge.py")
purge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(purge)
class CandidateTests(unittest.TestCase):
    def test_rewrite_only_disposable_copy_and_preserve_other_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d)
            work=root/"work"; work.mkdir()
            def g(*args):
                return subprocess.check_output(["git","-C",str(work),*args],stderr=subprocess.DEVNULL).strip()
            g("init","-q","-b","main")
            g("config","user.name","test"); g("config","user.email","test@example.invalid")
            (work/"opaque-target").write_bytes(b"old secret ciphertext")
            (work/"keep").write_bytes(b"unrelated ciphertext")
            g("add",".");g("commit","-qm","first")
            first=g("rev-parse","HEAD").decode()
            with self.assertRaises(ValueError):
                purge.prepare(work/".git",root/"blocked.git","main","opaque-target")
            g("rm","opaque-target");g("commit","-qm","delete live file")
            original=g("rev-parse","main").decode()
            blob=g("rev-parse",first+":opaque-target").decode()
            result=purge.prepare(work/".git",root/"candidate.git","main","opaque-target")
            self.assertFalse(result["published"])
            self.assertEqual(result["uniqueBlobsRemoved"],1)
            self.assertEqual(g("rev-parse","main").decode(),original)
            self.assertEqual(g("cat-file","blob",blob),b"old secret ciphertext")
            self.assertEqual(purge.git(root/"candidate.git","show","main:keep").stdout,b"unrelated ciphertext")
            self.assertNotEqual(purge.git(root/"candidate.git","cat-file","-e",blob,check=False).returncode,0)
if __name__ == "__main__": unittest.main()
