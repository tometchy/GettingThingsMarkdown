import importlib.util, pathlib, tempfile, unittest
spec = importlib.util.spec_from_file_location("budget", pathlib.Path(__file__).resolve().parents[2] / "server/sync/storage_budget.py")
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)
class StorageBudgetTests(unittest.TestCase):
    def test_live_and_history_count_and_no_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            for folder in ("files", "git", "recovery"): (root / folder).mkdir()
            (root / "files" / "data").write_bytes(b"x" * 4096)
            (root / "git" / "object").write_bytes(b"x" * 8192)
            (root / "recovery" / "identity").write_bytes(b"x" * 4096)
            value = budget.usage(root, 1024**3)
            self.assertEqual(value["liveBytes"], 4096)
            self.assertGreaterEqual(value["historyBytes"], 12288)
            self.assertIn("live-limit", budget.usage(root, 4096)["reasons"])
            self.assertIn("history-limit", budget.usage(root, 1024**3, 1024**3)["reasons"])
            self.assertEqual((root / "files" / "data").read_bytes(), b"x" * 4096)
if __name__ == "__main__": unittest.main()
