#!/usr/bin/env python3
"""Offline/stable-snapshot direct-I/O comparison and Git validation.

O_DIRECT bypasses the OS page cache where supported, not necessarily drive
or controller caches. No fallback to buffered reads for the direct pass.
Requires a stable, trusted repository snapshot; not a fencing mechanism.
"""
import argparse
import errno
import hashlib
import json
import mmap
import os
from pathlib import Path
import stat
import time
from git_integrity import scan, IntegrityError

CHUNK = 1024 * 1024


def signature(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def files(repo):
    repo = Path(repo)
    entries = {}
    def add(path):
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode):
            raise IntegrityError("non_regular_repository_file")
        entries[str(path.relative_to(repo))] = signature(st)
    for item in ("HEAD", "packed-refs", "shallow"):
        path = repo / item
        if path.exists() or path.is_symlink():
            add(path)
    for name in ("objects", "refs", "logs"):
        root = repo / name
        if not root.exists() and not root.is_symlink():
            continue
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise IntegrityError("non_regular_repository_directory")
        for current, dirs, names in os.walk(root, followlinks=False, onerror=lambda exc: (_ for _ in ()).throw(exc)):
            for directory in dirs:
                if not stat.S_ISDIR((Path(current) / directory).lstat().st_mode):
                    raise IntegrityError("non_regular_repository_directory")
            for filename in names:
                add(Path(current) / filename)
    return entries


def read_digest(path, expected, direct, deadline):
    if direct and not hasattr(os, "O_DIRECT"):
        raise IntegrityError("direct_io_unsupported")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    if direct:
        flags |= os.O_DIRECT
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if direct and exc.errno in (errno.EINVAL, errno.EOPNOTSUPP, errno.ENOSYS):
            raise IntegrityError("direct_io_unsupported") from exc
        raise
    try:
        if signature(os.fstat(fd)) != expected:
            raise IntegrityError("repository_changed")
        digest = hashlib.sha256()
        size = expected[2]
        offset = 0
        # mmap provides page-aligned memory; CHUNK is a multiple of common
        # logical/physical block sizes. An unsupported alignment fails closed.
        with mmap.mmap(-1, CHUNK) as buffer:
            while offset < size:
                if time.monotonic() >= deadline:
                    raise IntegrityError("physical_scrub_timeout")
                try:
                    count = os.preadv(fd, [buffer], offset)
                except OSError as exc:
                    if direct and exc.errno in (errno.EINVAL, errno.EOPNOTSUPP, errno.ENOSYS):
                        raise IntegrityError("direct_io_unsupported") from exc
                    raise
                if count != min(CHUNK, size - offset):
                    raise IntegrityError("short_or_changed_read")
                view = memoryview(buffer)[:count]
                try:
                    digest.update(view)
                finally:
                    view.release()
                offset += count
        if signature(os.fstat(fd)) != expected or signature(path.lstat()) != expected:
            raise IntegrityError("repository_changed")
        return digest.digest()
    finally:
        os.close(fd)


def scrub(repo, timeout=86400, expected_refs=None):
    repo = Path(repo).resolve(strict=True)
    started = time.monotonic()
    deadline = started + timeout
    before = files(repo)
    if "HEAD" not in before or not any(n.startswith("objects/") for n in before):
        raise IntegrityError("repository_missing")
    size = 0
    for relative, info in sorted(before.items()):
        path = repo / relative
        physical = read_digest(path, info, True, deadline)
        cached = read_digest(path, info, False, deadline)
        if physical != cached:
            raise IntegrityError("direct_buffered_digest_mismatch")
        size += info[2]
    if before != files(repo):
        raise IntegrityError("repository_changed")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise IntegrityError("physical_scrub_timeout")
    result = scan(repo, expected_refs, remaining)
    if before != files(repo):
        raise IntegrityError("repository_changed")
    return {**result, "directReadBytes": size, "files": len(before),
            "physicalDurationSeconds": round(time.monotonic() - started, 3),
            "pageCacheBypassRequested": True,
            "hardwareCacheBypassGuaranteed": False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("repository")
    p.add_argument("--timeout", type=int, default=86400)
    args = p.parse_args()
    try:
        print(json.dumps({"event": "gtmd_physical_scrub", "state": "verified",
                          **scrub(args.repository, args.timeout)}))
    except (IntegrityError, OSError, ValueError) as exc:
        print(json.dumps({"event": "gtmd_physical_scrub", "state": "unverified",
                          "reason": str(exc) if isinstance(exc, IntegrityError) else type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
