#!/usr/bin/env python3
"""Offline Sync state from the already verified Git replica.

No network fetch, new repository, Syncthing serve, user-data decryption or
live writes. The only decrypted secret is the infrastructure identity.
"""
import hashlib
import io
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET

from git_integrity import git, refs, IntegrityError, atomic_json
from worktree_integrity import verify, rows
from recovery_crypto import decode, read
from recovery_witness import require
from physical_scrub import scrub

MAX_IDENTITY = 4 * 1024 * 1024
DEVICE_ID = re.compile(r"[A-Z2-7]{7}(?:-[A-Z2-7]{7}){7}\Z")
IDENTITY_FILES = {"config.xml", "cert.pem", "key.pem"}
BRANCHES = {"refs/heads/gtm-tenant", "refs/heads/gtm-recovery"}


def command(args, timeout, *, data=None, output=None, env=None):
    try:
        p = subprocess.run(args, input=data, stdout=output if output is not None else subprocess.PIPE,
                           stderr=subprocess.DEVNULL, check=True, timeout=timeout, env=env)
        return p.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise IntegrityError("offline_restore_command_failed") from exc


def config(raw):
    require(isinstance(raw, dict) and set(raw) == {"deviceId", "folderId", "identityRef"}
            and isinstance(raw["deviceId"], str) and DEVICE_ID.fullmatch(raw["deviceId"])
            and isinstance(raw["folderId"], str) and 0 < len(raw["folderId"]) <= 128
            and raw["identityRef"] in BRANCHES, "invalid_sync_restore_inventory")
    return raw


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise IntegrityError("sync_restore_timeout")
    return value


def identity_bundle(repo, manifest, cfg, age_key, deadline):
    require(cfg["identityRef"] in manifest, "identity_ref_not_anchored")
    obj = manifest[cfg["identityRef"]] + ":identity.tar.gz.age"
    require(git(repo, "cat-file", "-t", obj, capture=True, timeout=remaining(deadline)).strip() == "blob",
            "identity_not_blob")
    count = int(git(repo, "cat-file", "-s", obj, capture=True, timeout=remaining(deadline)).strip())
    require(0 < count <= MAX_IDENTITY, "identity_bundle_too_large")
    with tempfile.TemporaryFile() as encrypted:
        git(repo, "cat-file", "blob", obj, output=encrypted, timeout=remaining(deadline))
        require(encrypted.tell() == count, "identity_blob_size_mismatch")
        encrypted.seek(0)
        # age output cannot exceed bounded ciphertext except for the
        # subsequent gzip layer, which is individually bounded below.
        try:
            p = subprocess.run(["age", "-d", "-i", str(age_key)], stdin=encrypted,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               check=True, timeout=remaining(deadline))
        except (OSError, subprocess.SubprocessError) as exc:
            raise IntegrityError("identity_decryption_failed") from exc
    require(0 < len(p.stdout) <= MAX_IDENTITY, "identity_bundle_too_large")
    result = {}
    # Stream archive members. Never extract tar paths or trust file modes.
    with tarfile.open(fileobj=io.BytesIO(p.stdout), mode="r|gz") as archive:
        for member in archive:
            require(member.name in IDENTITY_FILES and member.name not in result
                    and member.isfile() and 0 < member.size <= MAX_IDENTITY
                    and sum(map(len, result.values())) + member.size <= MAX_IDENTITY,
                    "invalid_identity_archive_member")
            f = archive.extractfile(member)
            require(f is not None, "identity_archive_missing_bytes")
            data = f.read(member.size+1)
            require(len(data) == member.size, "identity_archive_size_mismatch")
            result[member.name] = data
    require(set(result) == IDENTITY_FILES, "identity_archive_incomplete")
    return result


def validate_config(raw, cfg):
    require(b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper(),
            "unsupported_identity_xml")
    root = ET.fromstring(raw)
    require(root.tag == "configuration", "invalid_syncthing_configuration")
    folders = root.findall("folder")
    require(len(folders) == 1 and folders[0].get("id") == cfg["folderId"]
            and folders[0].get("type") == "receiveencrypted"
            and folders[0].get("path") == "/data/files", "unexpected_syncthing_folder")
    require(sum(d.get("id") == cfg["deviceId"] for d in root.findall("device")) == 1,
            "device_id_not_in_config")
    require(not any((x.text or "").strip() for x in root.iter("encryptionPassword")),
            "unexpected_plaintext_folder_secret")


def verify_identity(home, bundle, cfg, deadline):
    require(home.is_dir() and not home.is_symlink(), "invalid_identity_directory")
    require({p.name for p in home.iterdir()} == IDENTITY_FILES, "unexpected_identity_contents")
    for name, expected in bundle.items():
        path = home/name
        s = path.lstat()
        require(stat.S_ISREG(s.st_mode) and s.st_nlink == 1 and not s.st_mode & 0o077
                and s.st_size == len(expected) and path.read_bytes() == expected,
                "restored_identity_mismatch")
    validate_config(bundle["config.xml"], cfg)
    # Use the actual Syncthing CLI, not a hand-rolled Device ID algorithm.
    # Neither device-id nor OpenSSL launches a synchronization process.
    actual = command(["syncthing", "device-id", "--home="+str(home)],
                     min(30, remaining(deadline))).decode("ascii").strip()
    require(actual == cfg["deviceId"], "restored_device_id_mismatch")
    cert = command(["openssl", "x509", "-in", str(home/"cert.pem"), "-pubkey", "-noout"],
                   min(30, remaining(deadline)))
    key = command(["openssl", "pkey", "-in", str(home/"key.pem"), "-pubout"],
                  min(30, remaining(deadline)))
    require(cert == key, "restored_certificate_key_mismatch")


def restore_files(repo, files, main, deadline):
    # Use a disposable Git index, not a second clone or live repo index.
    # Refuse unsafe tree entries BEFORE checkout. Post-check raw blob hashes
    # catches any Git checkout transformation; no such tree is accepted.
    with tempfile.TemporaryFile() as listing:
        git(repo, "ls-tree", "-r", "-z", "--full-tree", main, output=listing,
            timeout=remaining(deadline))
        listing.seek(0)
        for entry in rows(listing):
            remaining(deadline)
            meta, sep, path = entry.partition(b"\t")
            bits = meta.split(b" ")
            require(sep and len(bits) == 3 and bits[0] in (b"100644", b"100755")
                    and bits[1] == b"blob" and all(p not in (b"", b".", b"..", b".git",
                    b".stfolder", b".stversions", b".stignore") and not p.startswith(b".syncthing.")
                    for p in path.lower().split(b"/")), "unsafe_sync_tree_entry")
    files.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(prefix="gtmd-sync-index-") as d:
        env = {k:v for k,v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_INDEX_FILE=str(Path(d)/"index"), GIT_CONFIG_NOSYSTEM="1",
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_REPLACE_OBJECTS="1",
                   GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
        base = ["git", "--git-dir", str(repo), "--work-tree", str(files),
                "-c", "core.autocrlf=false", "-c", "core.hooksPath=/dev/null",
                "-c", "protocol.allow=never", "-c", "gc.auto=0",
                "-c", "maintenance.auto=false"]
        command(base+["read-tree",main], remaining(deadline), env=env)
        command(base+["checkout-index","--all","--prefix="+str(files)+"/"],
                remaining(deadline), env=env)


def prepare(repo, dest, manifest, raw_cfg, age_key, timeout=86400):
    cfg = config(raw_cfg)
    repo, dest = Path(repo).resolve(strict=True), Path(dest)
    deadline = time.monotonic()+timeout
    require(not dest.is_symlink() and dest.is_absolute()
            and dest.parent.resolve(strict=True) == dest.parent
            and repo.parent == dest.parent and repo.name == "repository.git"
            and dest.name == "sync-state", "invalid_sync_staging_path")
    require(refs(repo, min(60, remaining(deadline))) == manifest, "sync_manifest_mismatch")
    main = manifest["refs/heads/main"]
    # The source is already scrubbed by the stage runner. Verify again when
    # resuming this independently triggered operation.
    scrub(repo, remaining(deadline), manifest)
    bundle = identity_bundle(repo, manifest, cfg, age_key, deadline)
    validate_config(bundle["config.xml"], cfg)
    marker = dest/"ready.json"
    identity = {"schema":1, "kind":"offline_sync_state", "main":main,
                "identityRef":cfg["identityRef"], "identityCommit":manifest[cfg["identityRef"]],
                "deviceId":cfg["deviceId"], "folderId":cfg["folderId"], "activationAllowed":False}
    if dest.exists():
        require(dest.is_dir() and not dest.is_symlink() and marker.is_file()
                and not marker.is_symlink() and decode(read(marker)) == identity
                and {p.name for p in dest.iterdir()} == {"files","syncthing","ready.json"},
                "incomplete_or_conflicting_sync_state")
    else:
        dest.mkdir(mode=0o700)
        home = dest/"syncthing"
        home.mkdir(mode=0o700)
        for name, data in bundle.items():
            fd = os.open(home/name, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,"wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        verify_identity(home,bundle,cfg,deadline)
        restore_files(repo,dest/"files",main,deadline)
    verify_identity(dest/"syncthing",bundle,cfg,deadline)
    checked = verify(repo,dest/"files",main,remaining(deadline))
    # No Syncthing index is fabricated, no technical files injected. The
    # later fenced start must safely rebuild the index before opening peers.
    require(refs(repo,min(60,remaining(deadline))) == manifest, "sync_manifest_changed")
    if not marker.exists():
        atomic_json(marker,identity)
    return {"state":"sync_state_prepared","files":checked["files"],
            "verifiedBytes":checked["verifiedBytes"],"deviceIdVerified":True,
            "activationAllowed":False}
