#!/usr/bin/env python3
"""Exercise the actual mail writer with real local A/B Git repositories."""
import importlib.util, os, pathlib, subprocess, tempfile, unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "server/sync/mail-files.py"

class GitMailWriterTests(unittest.TestCase):
    def test_store_retries_without_touching_notes(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            work = root / "files"
            work.mkdir()
            remote = root / "history.git"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            env = dict(os.environ, GIT_DIR=str(root / "git"), GIT_WORK_TREE=str(work))
            def git(*args):
                return subprocess.check_output(["git", *args], env=env).strip()
            git("init", "-q", "-b", "main")
            git("config", "user.name", "test")
            git("config", "user.email", "test@example.invalid")
            (work / "opaque").write_bytes(b"original notes")
            git("add", "opaque")
            git("commit", "-qm", "notes")
            git("remote", "add", "origin", str(remote))
            main = git("rev-parse", "main")
            os.environ["GTM_MAIL_DATA_ROOT"] = d
            os.environ["GTM_SYNCTHING_API_KEY"] = "test-only"
            spec = importlib.util.spec_from_file_location("mail_writer", SCRIPT)
            writer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(writer)
            digest = "a" * 64
            payload = b'{"ciphertext":"opaque-original"}'
            # A failed remote push must raise instead of confirming success.
            git("remote", "set-url", "origin", str(root / "missing.git"))
            with self.assertRaises(subprocess.CalledProcessError):
                writer.store(digest, payload)
            first = git("rev-parse", "gtm-mail")
            git("remote", "set-url", "origin", str(remote))
            self.assertEqual(writer.store(digest, b"different retry"), payload)
            self.assertEqual(first, git("rev-parse", "gtm-mail"))
            second = subprocess.check_output(["git", "--git-dir", str(remote),
                "rev-parse", "gtm-mail"]).strip()
            self.assertEqual(first, second)
            self.assertEqual(git("rev-parse", "main"), main)
            self.assertEqual(list(work.iterdir()), [work / "opaque"])
            self.assertEqual((work / "opaque").read_bytes(), b"original notes")
            self.assertEqual(git("status", "--porcelain"), b"")
            # An already accepted message can retry even above rate/quota.
            from unittest.mock import patch
            original_git = writer.git
            def full_rate(*args, **kwargs):
                if args[0] == "rev-list":
                    return subprocess.CompletedProcess(args, 0, b"x\n" * 100, b"")
                return original_git(*args, **kwargs)
            with patch.object(writer, "git", side_effect=full_rate):
                self.assertEqual(writer.store(digest, b"retry again"), payload)
                with self.assertRaises(writer.RateExceeded):
                    writer.store("b"*64, b"new message")
            self.assertEqual(first, git("rev-parse", "gtm-mail"))

if __name__ == "__main__":
    unittest.main()
