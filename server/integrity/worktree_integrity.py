#!/usr/bin/env python3
"""Verify ciphertext working tree against an independently expected Git main.

Use an offline or consistently fenced snapshot, not a live changing Syncthing
folder. Unknown files (including technical files) are not silently excluded.
No checkout, reset, decryption, write or automatic repair is performed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time

from git_integrity import git, refs, scan, IntegrityError, OID

CHUNK = 1024 * 1024


def rows(file):
    """Stream Git's NUL-separated tree entries, preserving raw path bytes."""
    pending = b""
    while True:
        chunk = file.read(CHUNK)
        if not chunk:
            break
        parts = (pending + chunk).split(b"\0")
        pending = parts.pop()
        if len(pending) > 65536:
            raise IntegrityError("tree_entry_too_large")
        yield from parts
    if pending:
        raise IntegrityError("unterminated_tree_entry")


def signature(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def hash_file(path, algorithm, deadline):
    # Do not follow symlinks or block indefinitely on special files.
    initial = os.lstat(path)
    if not stat.S_ISREG(initial.st_mode):
        raise IntegrityError("non_regular_worktree_file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        if signature(os.fstat(fd)) != signature(initial):
            raise IntegrityError("worktree_changed")
        h = hashlib.new(algorithm, usedforsecurity=True)
        h.update(b"blob " + str(initial.st_size).encode() + b"\0")
        size = 0
        with os.fdopen(fd, "rb", closefd=False) as file:
            while True:
                if time.monotonic() >= deadline:
                    raise IntegrityError("worktree_timeout")
                chunk = file.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                h.update(chunk)
        if size != initial.st_size or signature(os.fstat(fd)) != signature(initial) or signature(os.lstat(path)) != signature(initial):
            raise IntegrityError("worktree_changed")
        return h.hexdigest(), size
    finally:
        os.close(fd)


def verify(repo, worktree, expected_main, timeout=86400):
    if not isinstance(expected_main, str) or not OID.fullmatch(expected_main):
        raise IntegrityError("invalid_expected_main")
    repo, root = Path(repo).resolve(strict=True), Path(worktree).resolve(strict=True)
    if repo == root or repo in root.parents or root in repo.parents:
        raise IntegrityError("worktree_and_repository_must_be_separate")
    if not root.is_dir():
        raise IntegrityError("worktree_missing")
    started = time.monotonic()
    deadline = started + timeout
    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise IntegrityError("worktree_timeout")
        return budget
    before = refs(repo, min(60, remaining()))
    if before.get("refs/heads/main") != expected_main:
        raise IntegrityError("worktree_manifest_mismatch")
    algorithm = "sha256" if len(expected_main) == 64 else "sha1"
    total = count = 0
    # Disk-backed temporary path index, no ciphertext data stored here.
    # Directory names/paths are kept private; never log them.
    with tempfile.TemporaryDirectory(prefix="gtmd-tree-index-") as temp:
        db = sqlite3.connect(str(Path(temp) / "paths.sqlite"))
        try:
            db.execute("CREATE TABLE expected(path BLOB PRIMARY KEY, oid TEXT NOT NULL)")
            with tempfile.TemporaryFile() as listing:
                git(repo, "ls-tree", "-r", "-z", "--full-tree", expected_main,
                    timeout=remaining(), output=listing)
                listing.seek(0)
                for entry in rows(listing):
                    remaining()
                    meta, sep, path = entry.partition(b"\t")
                    bits = meta.split(b" ")
                    if (not sep or len(bits) != 3 or bits[0] not in (b"100644", b"100755") or
                        bits[1] != b"blob" or not path or
                        any(part in (b"", b".", b"..") for part in path.split(b"/"))):
                        raise IntegrityError("unsupported_git_tree_entry")
                    oid = bits[2].decode("ascii")
                    if not OID.fullmatch(oid) or len(oid) != len(expected_main):
                        raise IntegrityError("invalid_tree_oid")
                    db.execute("INSERT INTO expected VALUES (?,?)", (path, oid))
            db.commit()
            # os.walk with bytes preserves even non-UTF8 Git paths.
            root_bytes = os.fsencode(root)
            for current, dirs, files in os.walk(root_bytes, followlinks=False,
                    onerror=lambda exc: (_ for _ in ()).throw(exc)):
                remaining()
                for directory in dirs:
                    if not stat.S_ISDIR(os.lstat(os.path.join(current, directory)).st_mode):
                        raise IntegrityError("non_regular_worktree_directory")
                for filename in files:
                    path = os.path.join(current, filename)
                    relative = os.path.relpath(path, root_bytes)
                    expected = db.execute("SELECT oid FROM expected WHERE path=?", (relative,)).fetchone()
                    if expected is None:
                        raise IntegrityError("unexpected_worktree_file")
                    oid, size = hash_file(path, algorithm, deadline)
                    if oid != expected[0]:
                        raise IntegrityError("worktree_blob_mismatch")
                    db.execute("DELETE FROM expected WHERE path=?", (relative,))
                    count += 1
                    total += size
            if db.execute("SELECT 1 FROM expected LIMIT 1").fetchone():
                raise IntegrityError("missing_worktree_file")
        finally:
            db.close()
    # Full Git validation as a separate check; working-tree bytes alone are
    # not proof the historical object store is complete.
    scan(repo, before, remaining())
    return {"files": count, "verifiedBytes": total,
            "durationSeconds": round(time.monotonic() - started, 3)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("repository")
    p.add_argument("worktree")
    p.add_argument("--expected-main", required=True)
    p.add_argument("--timeout", type=int, default=86400)
    args = p.parse_args()
    try:
        print(json.dumps({"event": "gtmd_worktree_integrity", "state": "verified",
                          **verify(args.repository, args.worktree, args.expected_main, args.timeout)}))
    except (IntegrityError, OSError, ValueError, sqlite3.Error, UnicodeError) as exc:
        print(json.dumps({"event": "gtmd_worktree_integrity", "state": "unverified",
                          "reason": str(exc) if isinstance(exc, IntegrityError) else type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
