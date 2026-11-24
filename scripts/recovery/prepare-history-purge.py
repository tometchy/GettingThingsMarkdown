#!/usr/bin/env python3
"""Build a garbage-collected candidate in a NEW isolated mirror.

This is NOT the live purge worker. Never pushes, updates the original repo,
or authorizes destruction. A verified retention/fencing coordinator must
publish a candidate atomically and remove retired replicas separately.
"""
import argparse
import json
import os
import pathlib
import shutil
import subprocess
import tempfile

def git(repo, *args, data=None, index=None, check=True):
    env = dict(os.environ, GIT_LITERAL_PATHSPECS="1")
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        env.pop(k, None)
    if index: env["GIT_INDEX_FILE"] = str(index)
    return subprocess.run(["git", "--git-dir=" + str(repo), *args], input=data,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=check)

def refs(repo):
    result = git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    return dict(line.decode().split(" ", 1) for line in result.stdout.splitlines())

def target_blob(repo, oid, path):
    raw = git(repo, "ls-tree", "-z", "--full-tree", oid, "--", path).stdout
    if not raw: return None
    rows = raw.rstrip(b"\0").split(b"\0")
    if len(rows) != 1: raise ValueError("Path must refer to a single file")
    meta, name = rows[0].split(b"\t", 1)
    mode, kind, blob = meta.split()
    if name != path.encode() or kind != b"blob":
        raise ValueError("Only literal file paths are supported")
    return blob.decode()

def prepare(source, candidate, branch, path):
    if branch not in ("main", "gtm-mail"):
        raise ValueError("Only data branches may be rewritten")
    if not path or path.startswith("/") or "\\" in path or len(path.encode()) > 1024 or any(ord(c)<32 for c in path) or any(p in ("", ".", "..") for p in path.split("/")):
        raise ValueError("Invalid opaque relative path")
    if branch == "gtm-mail":
        import re
        if not re.fullmatch(r"\.gtmd/inbox/[a-f0-9]{64}\.json", path):
            raise ValueError("Only a single mail envelope may be purged")
    source = source.resolve(strict=True)
    candidate = candidate.resolve()
    if candidate.exists() or source == candidate or source in candidate.parents:
        raise ValueError("Candidate must be a new separate directory")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    before = refs(source)
    ref = "refs/heads/" + branch
    if ref not in before: raise ValueError("Missing data branch")
    if branch == "main" and target_blob(source, before[ref], path):
        raise ValueError("Remove the live working-directory file through Syncthing first")
    with tempfile.TemporaryDirectory(dir=candidate.parent) as d:
        stage = pathlib.Path(d) / "candidate.git"
        clean = dict(os.environ)
        for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
            clean.pop(k, None)
        subprocess.run(["git", "clone", "--mirror", "--no-local", str(source), str(stage)],
            env=clean, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if refs(stage) != before:
            raise ValueError("Source refs changed during clone; retry from a consistent snapshot")
        mapped, removed_blobs = {}, set()
        index = pathlib.Path(d) / "index"
        commits = git(stage, "rev-list", "--topo-order", "--reverse", ref).stdout.decode().splitlines()
        for oid in commits:
            blob = target_blob(stage, oid, path)
            content = git(stage, "cat-file", "commit", oid).stdout
            headers, message = content.split(b"\n\n", 1)
            lines = headers.split(b"\n")
            changed = False
            if blob:
                removed_blobs.add(blob)
                index.unlink(missing_ok=True)
                git(stage, "read-tree", oid, index=index)
                git(stage, "update-index", "-z", "--index-info",
                    data=b"0 " + b"0"*len(blob) + b"\t" + path.encode() + b"\0", index=index)
                new_tree = git(stage, "write-tree", index=index).stdout.strip()
            for i, line in enumerate(lines):
                if line.startswith(b"tree ") and blob:
                    replacement = b"tree " + new_tree
                elif line.startswith(b"parent "):
                    parent = line[7:].decode()
                    replacement = b"parent " + mapped.get(parent, parent).encode()
                else:
                    continue
                if line != replacement:
                    lines[i] = replacement
                    changed = True
            if changed:
                if any(line.startswith((b"gpgsig ", b"gpgsig-sha256 ", b"mergetag ")) for line in lines):
                    raise ValueError("Signed/mergetag commits require a dedicated verified rewrite")
                rewritten = b"\n".join(lines) + b"\n\n" + message
                mapped[oid] = git(stage, "hash-object", "-t", "commit", "-w", "--stdin", data=rewritten).stdout.decode().strip()
            else:
                mapped[oid] = oid
        if not removed_blobs: raise ValueError("No historical file matches the request")
        new = mapped[before[ref]]
        git(stage, "update-ref", ref, new, before[ref])
        # A tag or other branch may still retain the exact historical path.
        # Do not silently rewrite infrastructure/recovery refs.
        for name, oid in refs(stage).items():
            if name != ref and oid != before[name]: raise ValueError("Unexpected ref modification")
        # Prove selected path is absent from every reachable commit on this ref.
        for oid in git(stage, "rev-list", "--all").stdout.decode().splitlines():
            if target_blob(stage, oid, path):
                raise RuntimeError("Historical path still reachable through another branch or tag; refusing candidate")
        reachable = {l.split(b" ",1)[0].decode() for l in git(stage,"rev-list","--objects","--all").stdout.splitlines()}
        shared = sorted(removed_blobs & reachable)
        # This removes unreachable objects only inside the isolated candidate.
        git(stage, "reflog", "expire", "--expire=now", "--all")
        git(stage, "gc", "--prune=now")
        git(stage, "fsck", "--full")
        for blob in removed_blobs - set(shared):
            if git(stage, "cat-file", "-e", blob, check=False).returncode == 0:
                raise RuntimeError("Unreachable target blob retained in candidate")
        if refs(source) != before: raise ValueError("Source changed; discard candidate")
        # Disable push from the candidate (requires a separate explicit publisher).
        git(stage, "config", "--remove-section", "remote.origin")
        stage.rename(candidate)
        return {"sourceRef": before[ref], "candidateRef": new, "branch": branch,
            "uniqueBlobsRemoved": len(removed_blobs)-len(shared),
            "blobsStillSharedWithOtherPathsOrRefs": len(shared),
            "published": False}

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=pathlib.Path, required=True)
    p.add_argument("--candidate", type=pathlib.Path, required=True)
    p.add_argument("--branch", choices=["main","gtm-mail"], required=True)
    p.add_argument("--opaque-path", required=True)
    a = p.parse_args()
    print(json.dumps(prepare(a.source,a.candidate,a.branch,a.opaque_path),sort_keys=True))
