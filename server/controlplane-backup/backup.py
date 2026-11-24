#!/usr/bin/env python3
"""Encrypted shared-state backup, separate from tenant data. No live restore."""
import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone

DBS = ("accounts.sqlite", "tenants.sqlite")
MAX_CONFIG_FILE = 1024 * 1024

def hash_file(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()

def run(args, **kwargs):
    return subprocess.run(args, check=True, timeout=600, **kwargs)

def snapshot(source, target):
    # SQLite online backup handles WAL and concurrent writers consistently
    # within each DB. Never copy a running SQLite file with cp.
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=15) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst, pages=256, sleep=0.05)
            if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite snapshot integrity failure")

def bundle(data, config, output, recipients, scratch):
    if len(set(recipients)) < 2 or any(not re.fullmatch(r"age1[a-z0-9]{58,64}", x) for x in recipients):
        raise ValueError("Two independent age recipients required")
    with tempfile.TemporaryDirectory(dir=scratch) as d:
        stage = pathlib.Path(d)
        for name in DBS:
            if not (data / name).is_file():
                raise RuntimeError("Required database missing")
            snapshot(data / name, stage / name)
        if config and config.is_dir():
            # Explicitly mounted Kubernetes Secrets only; skip ..data internals.
            for group in config.iterdir():
                if not group.is_dir() or not re.fullmatch(r"[A-Za-z0-9_-]+", group.name):
                    continue
                for item in group.iterdir():
                    if not item.is_file() or not re.fullmatch(r"[A-Za-z0-9_.-]+", item.name) or item.name.startswith("."):
                        continue
                    if item.stat().st_size > MAX_CONFIG_FILE:
                        raise ValueError("Configuration item too large")
                    name = f"config--{group.name}--{item.name}"
                    (stage / name).write_bytes(item.read_bytes())
        manifest = {"schema": 1, "utc": datetime.now(timezone.utc).isoformat(),
            "files": {p.name: hash_file(p) for p in sorted(stage.iterdir())}}
        (stage / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
        args = ["age"]
        for recipient in recipients: args += ["-r", recipient]
        # Stream plaintext tar straight to age; no unencrypted archive on disk.
        with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as tmp:
            temp_path = pathlib.Path(tmp.name)
            try:
                tar = subprocess.Popen(["tar", "-C", str(stage), "-cf", "-", "."], stdout=subprocess.PIPE)
                try:
                    run(args, stdin=tar.stdout, stdout=tmp)
                finally:
                    tar.stdout.close()
                    status = tar.wait()
                if status != 0: raise RuntimeError("Archive creation failed")
                tmp.flush()
                os.fsync(tmp.fileno())
            except BaseException:
                temp_path.unlink(missing_ok=True)
                raise
        os.replace(temp_path, output)

def restore(bundle_path, identity, target, scratch):
    if target.exists():
        raise ValueError("Restore target must not exist; never overwrite live state")
    import tarfile
    with tempfile.TemporaryDirectory(dir=scratch) as d:
        stage = pathlib.Path(d)
        archive = stage / "backup.tar"
        run(["age", "-d", "-i", str(identity), "-o", str(archive), str(bundle_path)])
        extracted = stage / "files"
        extracted.mkdir(mode=0o700)
        with tarfile.open(archive) as tar:
            seen = set()
            for item in tar:
                if item.name == "." and item.isdir(): continue
                name = item.name.removeprefix("./")
                if not item.isfile() or not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in (".", "..") or name in seen:
                    raise ValueError("Unsafe backup member")
                seen.add(name)
                with tar.extractfile(item) as src, (extracted / name).open("xb") as dst:
                    shutil.copyfileobj(src, dst)
        manifest = json.loads((extracted / "manifest.json").read_text())
        if manifest.get("schema") != 1 or not all(n in manifest["files"] for n in DBS):
            raise ValueError("Incomplete backup")
        if set(manifest["files"]) != seen - {"manifest.json"}:
            raise ValueError("Unexpected backup files")
        for name, digest in manifest["files"].items():
            if hash_file(extracted / name) != digest:
                raise ValueError("Backup checksum mismatch")
        for name in DBS:
            with sqlite3.connect((extracted / name).as_uri() + "?mode=ro", uri=True) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Restored SQLite integrity failure")
        # Stage on the destination filesystem and publish atomically.
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent) as out:
            candidate = pathlib.Path(out) / "restore"
            shutil.copytree(extracted, candidate)
            candidate.chmod(0o700)
            for file in candidate.iterdir(): file.chmod(0o600)
            if target.exists(): raise ValueError("Restore target already exists")
            candidate.rename(target)

def s3_copy(path, endpoint, bucket, profile, scratch):
    if not endpoint.startswith("https://") or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ValueError("HTTPS endpoint and valid bucket required")
    key = "controlplane/" + path.name
    args = ["aws", "--profile", profile, "--endpoint-url", endpoint, "s3", "cp"]
    uri = "s3://" + bucket + "/" + key
    run(args + [str(path), uri, "--only-show-errors"], stdout=subprocess.DEVNULL)
    with tempfile.TemporaryDirectory(dir=scratch) as d:
        copy = pathlib.Path(d) / "verify.age"
        run(args + [uri, str(copy), "--only-show-errors"], stdout=subprocess.DEVNULL)
        if hash_file(copy) != hash_file(path):
            raise RuntimeError("Remote encrypted snapshot checksum mismatch")

def periodic():
    scratch = pathlib.Path(os.environ.get("GTM_BACKUP_SCRATCH", "/scratch"))
    recipients = os.environ["GTM_RECOVERY_AGE_RECIPIENT"].split(",")
    a = (os.environ["GTM_BACKUP_ENDPOINT_A"], os.environ["GTM_BACKUP_BUCKET_A"], "cloud-a")
    b = (os.environ["GTM_BACKUP_ENDPOINT_B"], os.environ["GTM_BACKUP_BUCKET_B"], "cloud-b")
    if a[0] == b[0]: raise ValueError("Independent provider endpoints required")
    interval = max(60, int(os.environ.get("GTM_BACKUP_INTERVAL_SECONDS", "300")))
    status = scratch / "last-dual-success"
    while True:
        try:
            with tempfile.TemporaryDirectory(dir=scratch) as d:
                path = pathlib.Path(d) / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex + ".age")
                bundle(pathlib.Path("/app/data"), pathlib.Path("/config"), path, recipients, scratch)
                s3_copy(path, *a, scratch)
                s3_copy(path, *b, scratch)
                status.write_text(str(int(time.time())))
                print("Control Plane encrypted backup verified at both destinations", flush=True)
        except Exception as ex:
            # Avoid leaking secret paths, credentials or object contents.
            print("Control Plane dual backup failed:", type(ex).__name__, flush=True)
        time.sleep(interval)

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create")
    for n in ("data", "output", "scratch"): p.add_argument("--" + n, required=True, type=pathlib.Path)
    p.add_argument("--config", type=pathlib.Path)
    p.add_argument("--recipient", action="append", required=True)
    p = sub.add_parser("restore")
    for n in ("bundle", "identity", "target", "scratch"): p.add_argument("--" + n, required=True, type=pathlib.Path)
    sub.add_parser("run")
    args = parser.parse_args()
    if args.command == "create":
        bundle(args.data, args.config, args.output, args.recipient, args.scratch)
    elif args.command == "restore":
        restore(args.bundle, args.identity, args.target, args.scratch)
    else: periodic()

if __name__ == "__main__": main()
