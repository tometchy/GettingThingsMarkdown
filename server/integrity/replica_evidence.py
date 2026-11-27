#!/usr/bin/env python3
"""Trusted replica-side attestation; requires a stable snapshot and scoped key.

No key should be accessible to the tenant or Git process. The agent verifies
the witness challenge, full repository contents, and append-only ancestry.
This does not itself fence concurrent writers or enforce WORM retention.
"""
import argparse
import json
from pathlib import Path
import time

from git_integrity import git, refs, scan, IntegrityError
from recovery_crypto import RecoveryError, read, decode, digest, sign, verify
from recovery_witness import refmap, require, ROLES


def attest(repo, challenge_envelope, witness_public, replica_key, tenant, role, cloud,
           clock=time.time, timeout=86400):
    ch = verify(challenge_envelope, witness_public)
    require(ch["tenant"] == tenant and role in ROLES and ch["kind"] in ("anchor", "repair")
            and ch["issuedUnix"] <= clock() < ch["expiresUnix"], "invalid_challenge")
    require(ch["kind"] != "repair" or ch["targetRole"] != role, "wrong_repair_source")
    started = time.monotonic()
    def remaining():
        seconds = min(timeout - (time.monotonic() - started), ch["expiresUnix"] - clock())
        require(seconds > 0, "attestation_timeout")
        return seconds
    current = refmap(refs(repo, min(60, remaining())))
    old = ch["previousRefs"]
    if ch["kind"] == "repair":
        require(current == old, "source_not_current_anchor")
    else:
        for ref, oid in old.items():
            require(ref in current, "previous_ref_missing")
            if ref.startswith("refs/heads/") and current[ref] != oid:
                # A nonzero exit is treated as failure, including missing
                # old commit or corrupted graph. No authorized purge here.
                try:
                    git(repo, "merge-base", "--is-ancestor", oid, current[ref], timeout=remaining())
                except IntegrityError as exc:
                    raise RecoveryError("non_fast_forward") from exc
            else:
                require(current[ref] == oid, "protected_ref_changed")
    scan(repo, current, remaining())
    verified = int(clock())
    require(verified < ch["expiresUnix"], "attestation_timeout")
    return sign({"schema": 1, "kind": "replica_scan", "tenant": tenant, "role": role,
                 "cloud": cloud, "challengeDigest": digest(ch), "refs": current,
                 "verifiedUnix": verified, "appendOnlyVerified": True}, replica_key)


def sign_receipt(path, key):
    # Caller must establish the receipt was emitted by the trusted staging
    # worker on the expected volume. Never expose this key to tenant pods.
    receipt = decode(read(path))
    require(receipt.get("state") == "staged_verified" and receipt.get("activationAllowed") is False,
            "invalid_staging_receipt")
    return sign(receipt, key)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("attest")
    for arg in ("repo", "challenge", "witness-public", "replica-key", "tenant", "role", "cloud"):
        c.add_argument("--" + arg, required=True)
    c = sub.add_parser("sign-receipt")
    c.add_argument("receipt")
    c.add_argument("replica_key")
    a = p.parse_args()
    try:
        if a.command == "attest":
            result = attest(a.repo, decode(read(a.challenge)), a.witness_public, a.replica_key,
                            a.tenant, a.role, a.cloud)
        else:
            result = sign_receipt(a.receipt, a.replica_key)
        print(json.dumps(result, separators=(",", ":")))
    except (RecoveryError, IntegrityError, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"event": "gtmd_replica_evidence", "state": "rejected",
                          "reason": str(exc) if isinstance(exc, (RecoveryError, IntegrityError)) else type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
