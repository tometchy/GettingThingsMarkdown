"""Independent, read-only logical Git accounting (ciphertext blob bytes).

The result is a snapshot, not a reservation or admission decision. Never
use it alone to acknowledge a write. All blobs in the selected history are
charged; do not trust user-controlled path names as exclusions.
"""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

OID = re.compile(rb"[0-9a-f]{40}|[0-9a-f]{64}")
PROTECTED = "refs/gtmd/protected/"

def measure(git_dir, timeout=120):
    env = dict(os.environ, GIT_DIR=str(Path(git_dir).resolve()),
               GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1")
    env.pop("GIT_WORK_TREE", None)
    def git(*args, **kwargs):
        return subprocess.run(["git", *args], env=env, check=True,
                              timeout=timeout, stderr=subprocess.DEVNULL, **kwargs)
    # Reject incomplete history rather than reporting a misleading low usage.
    if git("rev-parse", "--is-shallow-repository", stdout=subprocess.PIPE).stdout.strip() != b"false":
        raise ValueError("Shallow history is not supported")
    main = git("rev-parse", "--verify", "refs/heads/main^{commit}",
               stdout=subprocess.PIPE).stdout.strip()
    protected = git("for-each-ref", "--format=%(objectname)", PROTECTED,
                    stdout=subprocess.PIPE).stdout.splitlines()
    roots = [main, *protected]
    if not all(OID.fullmatch(oid) for oid in roots):
        raise ValueError("Invalid Git root")
    total = count = objects = 0
    # Disk-backed metadata, not blob contents; bounded RAM for large histories.
    with tempfile.TemporaryFile() as ids, tempfile.TemporaryFile() as info:
        git("rev-list", "--objects", "--no-object-names", "--stdin",
            input=b"\n".join(roots) + b"\n", stdout=ids)
        ids.seek(0)
        git("cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
            stdin=ids, stdout=info)
        ids.seek(0)
        info.seek(0)
        for expected in ids:
            row = info.readline().split()
            if len(row) != 3 or row[0] != expected.strip() or not OID.fullmatch(row[0]):
                raise ValueError("Missing or invalid Git object")
            size = int(row[2])
            if size < 0 or row[1] not in (b"blob", b"tree", b"commit", b"tag"):
                raise ValueError("Invalid Git object metadata")
            objects += 1
            if row[1] == b"blob":
                total += size
                count += 1
        if info.read(1):
            raise ValueError("Unexpected Git object metadata")
    return dict(logicalHistoryBytes=total, blobCount=count, objectCount=objects,
                mainRevision=main.decode(), protectedRevisions=[p.decode() for p in protected],
                representation="git-ciphertext-blob-bytes")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("git_dir")
    print(json.dumps(measure(parser.parse_args().git_dir)))
