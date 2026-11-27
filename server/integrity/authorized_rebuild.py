#!/usr/bin/env python3
"""Verify an externally signed staging ticket, rebuild and emit a receipt.

The signer/coordinator is NOT implemented here. This is not a fencing
authorization or a live volume activation. A signed ticket is insufficient
for destructive repair; the coordinator must re-check current generation,
retention and fencing before any future cutover.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from git_integrity import IntegrityError, load_manifest, stage_rebuild, atomic_json
from physical_scrub import scrub

ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
TENANT = re.compile(r"gtm-[0-9a-f]{32}\Z")
MAX_TICKET = 4 * 1024 * 1024
FIELDS = {"schema", "operation", "tenant", "generation", "issuedUnix",
          "expiresUnix", "sourceRole", "sourceCloud", "sourceUrl",
          "targetRole", "targetCloud", "targetPvcUid", "refs"}


def unique_object(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise IntegrityError("duplicate_ticket_field")
        obj[k] = v
    return obj


def verify_ticket(ticket_path, sig_path, public_key, *, tenant, generation,
                  target_role, target_cloud, target_pvc_uid, now=None,
                  allow_local_test_source=False):
    now = time.time() if now is None else now
    def bounded(path, size):
        with open(path, "rb") as src:
            data = src.read(size + 1)
        if not data or len(data) > size:
            raise IntegrityError("invalid_ticket_size")
        return data
    raw = bounded(ticket_path, MAX_TICKET)
    sig = bounded(sig_path, 1024)
    key = bounded(public_key, 4096)
    if len(sig) != 64:
        raise IntegrityError("ticket_signature_invalid")
    # Verify the exact bytes parsed below; don't verify one file and later
    # reread potentially changed bytes. Pinned key comes from separate trust.
    with tempfile.TemporaryDirectory() as root:
        root = Path(root)
        for n, data in (("ticket", raw), ("signature", sig), ("key", key)):
            (root / n).write_bytes(data)
        try:
            der = subprocess.check_output(["openssl", "pkey", "-pubin", "-in",
                                           str(root / "key"), "-outform", "DER"],
                                          timeout=30, stderr=subprocess.DEVNULL)
            # RFC 8410 Ed25519 SubjectPublicKeyInfo: fixed OID and 32-byte key.
            if len(der) != 44 or not der.startswith(bytes.fromhex("302a300506032b6570032100")):
                raise IntegrityError("ticket_key_not_ed25519")
            subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin",
                            "-inkey", str(root / "key"), "-rawin", "-in", str(root / "ticket"),
                            "-sigfile", str(root / "signature")],
                           check=True, timeout=30, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            raise IntegrityError("ticket_signature_invalid") from exc
        t = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(t, dict) or set(t) != FIELDS or type(t["schema"]) is not int or t["schema"] != 1:
            raise IntegrityError("invalid_ticket_schema")
        for k in ("operation", "sourceCloud", "targetCloud", "targetPvcUid"):
            if not isinstance(t[k], str) or not ID.fullmatch(t[k]):
                raise IntegrityError("invalid_ticket_identity")
        if (not isinstance(t["tenant"], str) or not TENANT.fullmatch(t["tenant"]) or
                t["tenant"] != tenant or t["targetPvcUid"] != target_pvc_uid or
                t["targetRole"] != target_role or t["targetCloud"] != target_cloud or
                t["targetRole"] not in ("sync", "history") or
                t["sourceRole"] != ("sync" if target_role == "history" else "history") or
                t["sourceCloud"] == t["targetCloud"]):
            raise IntegrityError("ticket_target_mismatch")
        if (type(t["generation"]) is not int or t["generation"] <= 0 or
                type(generation) is not int or generation != t["generation"]):
            raise IntegrityError("ticket_generation_mismatch")
        if (type(t["issuedUnix"]) is not int or type(t["expiresUnix"]) is not int or
                not t["issuedUnix"] <= now < t["expiresUnix"] or
                not 0 < t["expiresUnix"] - t["issuedUnix"] <= 3600):
            raise IntegrityError("ticket_expired_or_invalid")
        if not isinstance(t["sourceUrl"], str):
            raise IntegrityError("invalid_source_url")
        url = urlsplit(t["sourceUrl"])
        local = allow_local_test_source and not url.scheme and Path(t["sourceUrl"]).is_absolute()
        if not local and (url.scheme != "https" or not url.hostname or
                          url.username or url.password or url.query or url.fragment):
            raise IntegrityError("invalid_source_url")
        refs = root / "refs.json"
        refs.write_text(json.dumps(t["refs"]))
        t["refs"] = load_manifest(refs)
        # Current MVP requires all four logical branches. Do not stage an
        # incomplete legacy split repository as a complete repair candidate.
        if not {"refs/heads/main", "refs/heads/gtm-tenant", "refs/heads/gtm-vault",
                "refs/heads/gtm-mail"} <= set(t["refs"]):
            raise IntegrityError("required_branches_missing")
    return t, hashlib.sha256(raw).hexdigest()


def execute(ticket, digest, staging, timeout=86400, physical=False):
    staging = Path(staging)
    # A uniquely named new staging root. Replays cannot overwrite it.
    staging.mkdir(mode=0o700)
    repo = staging / "repository.git"
    started = time.monotonic()
    result = stage_rebuild(ticket["sourceUrl"], repo, ticket["refs"], timeout)
    if physical:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise IntegrityError("rebuild_timeout")
        result = scrub(repo, remaining, ticket["refs"])
    receipt = {"schema": 1, "operation": ticket["operation"], "tenant": ticket["tenant"],
               "generation": ticket["generation"], "targetPvcUid": ticket["targetPvcUid"],
               "ticketSha256": digest, "refsVerified": len(ticket["refs"]),
               "completedUnix": int(time.time()), "physicalScrub": physical,
               "activationAllowed": False, "state": "staged_verified",
               "durationSeconds": round(time.monotonic() - started, 3)}
    atomic_json(staging / "receipt.json", receipt)
    return receipt


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("ticket", "signature", "public-key", "tenant", "target-role",
                "target-cloud", "target-pvc-uid", "staging"):
        p.add_argument("--" + key, required=True)
    p.add_argument("--generation", required=True, type=int)
    p.add_argument("--timeout", type=int, default=86400)
    p.add_argument("--physical-scrub", action="store_true")
    args = p.parse_args()
    try:
        ticket, digest = verify_ticket(args.ticket, args.signature, args.public_key,
            tenant=args.tenant, generation=args.generation, target_role=args.target_role,
            target_cloud=args.target_cloud, target_pvc_uid=args.target_pvc_uid)
        print(json.dumps(execute(ticket, digest, args.staging, args.timeout, args.physical_scrub)))
    except (IntegrityError, ValueError, TypeError, OSError) as exc:
        print(json.dumps({"event": "gtmd_authorized_rebuild", "state": "failed",
                          "reason": str(exc) if isinstance(exc, IntegrityError) else type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
