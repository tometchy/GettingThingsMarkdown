#!/usr/bin/env python3
"""Independent single-authority recovery metadata witness, CLI reference.

Only metadata is stored, never Git objects, credentials or user plaintext.
A durable generation is NOT a writer fence. No live activation is authorized.
Deploy on a separate trust domain; restoring an older witness disk snapshot
is not safe without an externally protected monotonic watermark.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
from urllib.parse import urlsplit

from recovery_crypto import RecoveryError, canonical, decode, digest, fingerprint, read, sign, verify, sign_bytes

TENANT = re.compile(r"gtm-[0-9a-f]{32}\Z")
ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
ROLES = ("sync", "history")
REQUIRED = {"refs/heads/" + b for b in ("main", "gtm-tenant", "gtm-vault", "gtm-mail")}
TTL = 600
TICKET_TTL = 900


def require(condition, reason):
    if not condition:
        raise RecoveryError(reason)


def identifier(v):
    return isinstance(v, str) and bool(ID.fullmatch(v))


def refmap(value):
    require(isinstance(value, dict) and REQUIRED <= set(value), "required_branches_missing")
    require(len(value) <= 10000, "too_many_refs")
    for ref, oid in value.items():
        require(isinstance(ref, str) and ref.startswith("refs/") and not ref.startswith("refs/replace/")
                and not any(c in ref for c in (" ", "~", "^", ":", "?", "*", "[", "\\", "..", "@{"))
                and all(p and not p.startswith(".") and not p.endswith(".lock") for p in ref.split("/"))
                and not ref.endswith(".") and all(32 < ord(c) < 127 for c in ref)
                and isinstance(oid, str) and OID.fullmatch(oid), "invalid_ref")
    require(len({len(v) for v in value.values()}) == 1, "mixed_object_format")
    return value


def config(path, witness_key):
    data = decode(read(path))
    require(isinstance(data, dict) and set(data) == {"tenants"}, "invalid_config")
    witness_fp = fingerprint(witness_key, True)
    result = {}
    for tenant, roles in data["tenants"].items():
        require(TENANT.fullmatch(tenant) and set(roles) == set(ROLES), "invalid_tenant")
        fps = {witness_fp}
        clouds = set()
        result[tenant] = {}
        for role, entry in roles.items():
            require(set(entry) == {"cloud", "publicKey", "sourceUrl"} and identifier(entry["cloud"]), "invalid_replica_config")
            url = urlsplit(entry["sourceUrl"])
            require(url.scheme == "https" and url.hostname and not url.username and not url.password
                    and not url.query and not url.fragment, "invalid_source_url")
            require(entry["cloud"] not in clouds, "replicas_not_independent")
            key = str(Path(entry["publicKey"]).resolve(strict=True))
            fp = fingerprint(key)
            require(fp not in fps, "replica_keys_not_independent")
            fps.add(fp)
            clouds.add(entry["cloud"])
            result[tenant][role] = {**entry, "publicKey": key, "fingerprint": fp}
    require(bool(result), "empty_config")
    return result


class Witness:
    def __init__(self, root, cfg, key, clock=time.time):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=False, exist_ok=True)
        st = self.root.lstat()
        require(stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and st.st_mode & 0o077 == 0,
                "insecure_witness_directory")
        self.cfg, self.key, self.clock = cfg, key, clock

    def now(self):
        return int(self.clock())

    @contextlib.contextmanager
    def transaction(self, tenant):
        require(tenant in self.cfg and TENANT.fullmatch(tenant), "unknown_tenant")
        lock = self.root / (tenant + ".lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            path = self.root / (tenant + ".json")
            if path.exists():
                # Corrupt metadata fails closed; no implicit reinitialization.
                state = decode(read(path))
                require(state.get("schema") == 1 and state.get("tenant") == tenant, "invalid_witness_state")
            else:
                state = {"schema": 1, "tenant": tenant, "generation": 0,
                         "refs": {}, "anchorDigest": None, "challenge": None,
                         "repair": None, "lastClosedRepair": None, "revision": 0}
            yield state
            state["revision"] += 1
            self.write(path, canonical(state))
        finally:
            os.close(fd)

    def write(self, path, raw):
        with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as f:
            tmp = f.name
            try:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        try:
            os.replace(tmp, path)
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def status(self, tenant):
        # Read with same lock; no state-changing command required.
        require(tenant in self.cfg and TENANT.fullmatch(tenant), "unknown_tenant")
        path = self.root / (tenant + ".json")
        if not path.exists():
            return {"tenant": tenant, "generation": 0, "state": "uninitialized"}
        state = decode(read(path))
        require(state.get("tenant") == tenant, "invalid_witness_state")
        return state

    def challenge(self, tenant, kind, target_role=None, target_pvc=None):
        with self.transaction(tenant) as s:
            now = self.now()
            require(kind in ("anchor", "repair"), "invalid_challenge_kind")
            active = s["challenge"]
            require(active is None or active["expiresUnix"] <= now, "challenge_already_active")
            require(s["repair"] is None, "repair_already_active")
            if kind == "repair":
                require(s["generation"] > 0 and target_role in ROLES and identifier(target_pvc), "invalid_repair_target")
            else:
                require(target_role is None and target_pvc is None, "invalid_anchor_target")
            challenge = {"schema": 1, "kind": kind, "tenant": tenant, "nonce": secrets.token_hex(32),
                         "generation": s["generation"], "anchorDigest": s["anchorDigest"],
                         "previousRefs": s["refs"], "issuedUnix": now, "expiresUnix": now + TTL,
                         "targetRole": target_role, "targetPvcUid": target_pvc}
            s["challenge"] = challenge
            return sign(challenge, self.key)

    def evidence(self, s, role, envelope):
        challenge = s["challenge"]
        require(challenge is not None and self.now() < challenge["expiresUnix"], "challenge_expired")
        entry = self.cfg[s["tenant"]][role]
        data = verify(envelope, entry["publicKey"])
        require(isinstance(data, dict) and set(data) == {"schema", "kind", "tenant", "role", "cloud",
                "challengeDigest", "refs", "verifiedUnix", "appendOnlyVerified"}, "invalid_evidence_schema")
        require(data["schema"] == 1 and data["kind"] == "replica_scan" and data["tenant"] == s["tenant"]
                and data["role"] == role and data["cloud"] == entry["cloud"]
                and data["challengeDigest"] == digest(challenge), "evidence_identity_mismatch")
        require(type(data["verifiedUnix"]) is int and challenge["issuedUnix"] <= data["verifiedUnix"] <= self.now(),
                "evidence_time_invalid")
        require(data["appendOnlyVerified"] is True, "append_only_not_verified")
        return refmap(data["refs"])

    def anchor(self, tenant, sync, history):
        with self.transaction(tenant) as s:
            ch = s["challenge"]
            require(ch is not None and ch["kind"] == "anchor" and s["repair"] is None, "no_anchor_challenge")
            a, b = self.evidence(s, "sync", sync), self.evidence(s, "history", history)
            require(a == b, "replicas_disagree")
            # No branch deletion or non-head rewrite in this phase. Actual
            # ancestry is attested by both independent agents.
            for ref, oid in s["refs"].items():
                require(ref in a and (ref.startswith("refs/heads/") or a[ref] == oid), "protected_ref_changed")
            generation = s["generation"] + 1
            record = {"schema": 1, "kind": "generation", "tenant": tenant, "generation": generation,
                      "previousDigest": s["anchorDigest"], "refsDigest": digest(a), "committedUnix": self.now()}
            s.update(generation=generation, refs=a, anchorDigest=digest(record), challenge=None)
            s["anchor"] = sign(record, self.key)
            return s["anchor"]

    def start_repair(self, tenant, evidence):
        with self.transaction(tenant) as s:
            ch = s["challenge"]
            require(ch is not None and ch["kind"] == "repair" and s["repair"] is None, "no_repair_challenge")
            target = ch["targetRole"]
            source = "history" if target == "sync" else "sync"
            require(self.evidence(s, source, evidence) == s["refs"], "source_not_current_anchor")
            op = "repair-" + secrets.token_hex(16)
            s["repair"] = {"operation": op, "generation": s["generation"], "anchorDigest": s["anchorDigest"],
                           "sourceRole": source, "targetRole": target, "targetPvcUid": ch["targetPvcUid"],
                           "state": "authorized", "attempt": 0, "ticket": None, "receipt": None,
                           "createdUnix": self.now()}
            s["challenge"] = None
            return dict(s["repair"])

    def ticket(self, tenant, operation):
        with self.transaction(tenant) as s:
            repair = s["repair"]
            require(repair and repair["operation"] == operation and repair["state"] in ("authorized", "ticket_issued"),
                    "repair_not_authorized")
            require(repair["generation"] == s["generation"] and repair["anchorDigest"] == s["anchorDigest"],
                    "repair_generation_changed")
            now = self.now()
            # Retry only after old ticket expiration; no overlapping attempts.
            if repair["ticket"] and now < repair["ticket"]["expiresUnix"]:
                t = repair["ticket"]
            else:
                repair["attempt"] += 1
                a, b = self.cfg[tenant][repair["sourceRole"]], self.cfg[tenant][repair["targetRole"]]
                t = {"schema": 1, "operation": operation + "-" + str(repair["attempt"]), "tenant": tenant,
                     "generation": s["generation"], "issuedUnix": now, "expiresUnix": now + TICKET_TTL,
                     "sourceRole": repair["sourceRole"], "sourceCloud": a["cloud"], "sourceUrl": a["sourceUrl"],
                     "targetRole": repair["targetRole"], "targetCloud": b["cloud"],
                     "targetPvcUid": repair["targetPvcUid"], "refs": s["refs"]}
                repair["ticket"] = t
                repair["state"] = "ticket_issued"
            raw = canonical(t)
            return raw, sign_bytes(raw, self.key)

    def receipt(self, tenant, operation, envelope):
        with self.transaction(tenant) as s:
            repair = s["repair"]
            require(repair and repair["operation"] == operation and repair["state"] in ("ticket_issued", "staged_verified"),
                    "no_active_ticket")
            t = repair["ticket"]
            data = verify(envelope, self.cfg[tenant][repair["targetRole"]]["publicKey"])
            expected = {"schema": 1, "operation": t["operation"], "tenant": tenant,
                        "generation": t["generation"], "targetPvcUid": t["targetPvcUid"],
                        "ticketSha256": hashlib.sha256(canonical(t)).hexdigest(),
                        "refsVerified": len(t["refs"]), "activationAllowed": False, "state": "staged_verified"}
            require(isinstance(data, dict) and all(data.get(k) == v for k, v in expected.items())
                    and data.get("activationAllowed") is False and data.get("physicalScrub") is True,
                    "receipt_mismatch_or_no_physical_scrub")
            require(type(data.get("completedUnix")) is int and t["issuedUnix"] <= data["completedUnix"] <= self.now(),
                    "receipt_time_invalid")
            if repair["state"] == "staged_verified":
                require(data == repair["receipt"], "receipt_conflict")
            repair["receipt"] = data
            repair["state"] = "staged_verified"
            # Receipt does not unlock generation changes or authorize cutover.
            return {"operation": operation, "state": repair["state"], "activationAllowed": False}

    def abort(self, tenant, operation):
        with self.transaction(tenant) as s:
            repair = s["repair"]
            require(repair and repair["operation"] == operation, "unknown_repair")
            require(not repair["ticket"] or self.now() >= repair["ticket"]["expiresUnix"],
                    "ticket_still_valid")
            s["lastClosedRepair"] = {"operation": operation, "generation": repair["generation"],
                                     "state": "aborted", "closedUnix": self.now()}
            s["repair"] = None
            return s["lastClosedRepair"]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("state-dir", "config", "key", "tenant"):
        p.add_argument("--" + key, required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    c = sub.add_parser("challenge")
    c.add_argument("kind", choices=("anchor", "repair"))
    c.add_argument("--target-role", choices=ROLES)
    c.add_argument("--target-pvc")
    c = sub.add_parser("anchor")
    c.add_argument("sync")
    c.add_argument("history")
    c = sub.add_parser("start-repair")
    c.add_argument("evidence")
    c = sub.add_parser("ticket")
    c.add_argument("operation")
    c.add_argument("output")
    c = sub.add_parser("receipt")
    c.add_argument("operation")
    c.add_argument("evidence")
    c = sub.add_parser("abort")
    c.add_argument("operation")
    args = p.parse_args()
    try:
        w = Witness(args.state_dir, config(args.config, args.key), args.key)
        tenant = args.tenant
        if args.cmd == "status":
            s = w.status(tenant)
            result = {"tenant": tenant, "generation": s["generation"], "revision": s.get("revision", 0),
                      "repairState": (s.get("repair") or {}).get("state"),
                      "activationAllowed": False}
        elif args.cmd == "challenge":
            result = w.challenge(tenant, args.kind, args.target_role, args.target_pvc)
        elif args.cmd == "anchor":
            result = w.anchor(tenant, decode(read(args.sync)), decode(read(args.history)))
        elif args.cmd == "start-repair":
            result = w.start_repair(tenant, decode(read(args.evidence)))
        elif args.cmd == "ticket":
            raw, sig = w.ticket(tenant, args.operation)
            out = Path(args.output)
            out.mkdir(mode=0o700)
            (out / "ticket.json").write_bytes(raw)
            (out / "ticket.sig").write_bytes(sig)
            result = {"state": "ticket_issued", "activationAllowed": False}
        elif args.cmd == "receipt":
            result = w.receipt(tenant, args.operation, decode(read(args.evidence)))
        else:
            result = w.abort(tenant, args.operation)
        print(json.dumps(result, separators=(",", ":")))
    except (RecoveryError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"event": "gtmd_recovery_witness", "state": "rejected",
                          "reason": str(exc) if isinstance(exc, RecoveryError) else type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
