"""Witness-side writer admission and freeze protocol.

A permit is a short-lived authorization, NOT proof that a remote process
actually stopped. The independent replica keys must attest stop/isolation.
No activation or replacement writer enrollment is implemented here.
"""
import secrets
from recovery_crypto import sign, verify, digest
from recovery_witness import require, identifier, ROLES

PERMIT_TTL = 60
CLOCK_SKEW = 5
STOP_EVIDENCE_TTL = 600


def state(s):
    value = s.get("writerFence")
    require(isinstance(value, dict), "writer_fencing_not_enrolled")
    return value


def identity(value):
    require(isinstance(value, dict) and set(value) == {"podUid", "pvcUids", "nodeUid"}
            and identifier(value["podUid"]) and identifier(value["nodeUid"])
            and isinstance(value["pvcUids"], list) and 1 <= len(value["pvcUids"]) <= 8
            and all(identifier(x) for x in value["pvcUids"])
            and value["pvcUids"] == sorted(set(value["pvcUids"])), "invalid_writer_identity")
    return value


def enroll(w, tenant, writers):
    """Trusted OFFLINE provisioning only. Never reset a prior enrollment."""
    require(isinstance(writers, dict) and set(writers) == set(ROLES), "invalid_writer_pair")
    writers = {role: identity(v) for role, v in writers.items()}
    require(writers["sync"]["podUid"] != writers["history"]["podUid"]
            and not set(writers["sync"]["pvcUids"]) & set(writers["history"]["pvcUids"]), "writer_identity_collision")
    with w.transaction(tenant) as s:
        require(s.get("writerFence") is None and s["repair"] is None and s["challenge"] is None,
                "writer_fencing_already_enrolled_or_busy")
        s["writerFence"] = {"epoch": 1, "phase": "open", "writers": writers,
                            "leases": {}, "freeze": None}
        return {"epoch": 1, "phase": "open", "activationAllowed": False}


def permit(w, tenant, role, envelope):
    require(role in ROLES, "invalid_role")
    # The coordinator certificate alone cannot request a lease for an
    # arbitrary writer: require the independently pinned role key.
    data = verify(envelope, w.cfg[tenant][role]["publicKey"])
    require(isinstance(data, dict) and set(data) == {"schema", "kind", "tenant", "role",
            "epoch", "identity", "requestId", "issuedUnix"}, "invalid_lease_request")
    require(data["schema"] == 1 and data["kind"] == "writer_permit_request"
            and data["tenant"] == tenant and data["role"] == role
            and identifier(data["requestId"]), "lease_request_identity_mismatch")
    with w.transaction(tenant) as s:
        f = state(s)
        now = w.now()
        require(f["phase"] == "open" and s["repair"] is None, "writer_frozen")
        require(data["epoch"] == f["epoch"] and identity(data["identity"]) == f["writers"][role],
                "writer_identity_or_epoch_mismatch")
        require(type(data["issuedUnix"]) is int and now - 15 <= data["issuedUnix"] <= now + CLOCK_SKEW,
                "lease_request_stale")
        previous = f["leases"].get(role)
        if previous:
            if data["requestId"] == previous["requestId"]:
                # Exact duplicate is idempotent; never extend its deadline.
                require(digest(data) == previous["requestDigest"], "lease_request_conflict")
                return previous["envelope"]
            require(data["issuedUnix"] > previous["requestUnix"], "lease_request_replayed")
        payload = {"schema": 1, "kind": "writer_permit", "tenant": tenant, "role": role,
                   "epoch": f["epoch"], "identity": f["writers"][role],
                   "sequence": (previous["sequence"] + 1 if previous else 1),
                   "issuedUnix": now, "expiresUnix": now + PERMIT_TTL}
        result = sign(payload, w.key)
        f["leases"][role] = {"requestId": data["requestId"], "requestDigest": digest(data),
                             "requestUnix": data["issuedUnix"], "sequence": payload["sequence"],
                             "expiresUnix": payload["expiresUnix"], "envelope": result}
        return result


def freeze(w, tenant, incident, target_role=None, target_pvc=None):
    require(identifier(incident), "invalid_incident")
    require((target_role is None and target_pvc is None) or
            (target_role in ROLES and identifier(target_pvc)), "invalid_repair_plan")
    with w.transaction(tenant) as s:
        f = state(s)
        plan = None if target_role is None else {"targetRole": target_role, "targetPvcUid": target_pvc,
                   "generation": s["generation"], "anchorDigest": s["anchorDigest"]}
        if plan is not None:
            require(s["generation"] > 0 and target_pvc not in {uid for v in f["writers"].values() for uid in v["pvcUids"]},
                    "repair_requires_new_volume_and_anchor")
        if f["phase"] != "open":
            require(f["freeze"]["incident"] == incident, "different_freeze_active")
            existing = f["freeze"].get("plan")
            # Freeze immediately, even if no spare capacity is available.
            # Attach exactly one target later in the same witness CAS; never
            # change the signed challenge, epoch, deadline or stop proofs.
            if plan is not None:
                require(existing == plan or (existing is None and s["repair"] is None
                        and s["challenge"] is None), "different_freeze_active")
                if existing is None:
                    f["freeze"]["plan"] = plan
            return sign(f["freeze"]["challenge"], w.key)
        require(s["repair"] is None, "witness_busy")
        # An emergency freeze supersedes a pending anchor scan; its evidence
        # can no longer commit. Never wait 48h for an abandoned challenge.
        s["challenge"] = None
        now = w.now()
        prior = f["epoch"]
        f["epoch"] += 1
        # Wait out every previously issued permit, including clock skew.
        # This waiting period is necessary but never sufficient for fencing.
        deadline = max([now] + [v["expiresUnix"] for v in f["leases"].values()]) + CLOCK_SKEW
        challenge = {"schema": 1, "kind": "writer_freeze", "tenant": tenant,
                     "incident": incident, "nonce": secrets.token_hex(32),
                     "previousEpoch": prior, "epoch": f["epoch"],
                     "writers": f["writers"], "notBeforeUnix": deadline,
                     "issuedUnix": now}
        f["phase"] = "freezing"
        f["freeze"] = {"incident": incident, "challenge": challenge, "proofs": {}, "plan": plan}
        return sign(challenge, w.key)


def confirm_stop(w, tenant, role, envelope):
    require(role in ROLES, "invalid_role")
    data = verify(envelope, w.cfg[tenant][role]["publicKey"])
    with w.transaction(tenant) as s:
        f = state(s)
        require(f["phase"] in ("freezing", "frozen"), "freeze_not_active")
        ch = f["freeze"]["challenge"]
        expected = {"schema": 1, "kind": "writer_stopped", "tenant": tenant, "role": role,
                    "challengeDigest": digest(ch), "identity": ch["writers"][role],
                    "epoch": ch["epoch"], "processesStopped": True,
                    "writeAccessRevoked": True, "oldVolumesIsolated": True}
        require(isinstance(data, dict) and set(data) == set(expected) | {"verifiedUnix", "method", "evidenceDigest"}
                and all(data.get(k) == v for k, v in expected.items())
                and all(data[k] is True for k in ("processesStopped", "writeAccessRevoked", "oldVolumesIsolated")),
                "stop_evidence_mismatch")
        require(data["method"] in ("trusted_host_runtime", "provider_fence")
                and isinstance(data["evidenceDigest"], str) and len(data["evidenceDigest"]) == 64
                and all(c in "0123456789abcdef" for c in data["evidenceDigest"]), "stop_evidence_missing")
        now = w.now()
        require(type(data["verifiedUnix"]) is int and ch["notBeforeUnix"] <= data["verifiedUnix"] <= now
                and now - data["verifiedUnix"] <= STOP_EVIDENCE_TTL, "stop_evidence_stale_or_early")
        old = f["freeze"]["proofs"].get(role)
        if old:
            require(old == envelope, "stop_evidence_conflict")
        f["freeze"]["proofs"][role] = envelope
        if set(f["freeze"]["proofs"]) == set(ROLES):
            f["phase"] = "frozen"
        return {"epoch": f["epoch"], "phase": f["phase"], "activationAllowed": False}


def require_repair_frozen(s, target_role=None, target_pvc=None):
    # Legacy reference staging can remain non-activating. Once enrolled,
    # no new repair challenge or ticket may bypass the writer freeze.
    if s.get("writerFence") is not None:
        f = state(s)
        require(f["phase"] == "frozen", "writers_not_confirmed_frozen")
        plan = f["freeze"].get("plan")
        require(plan is not None and plan["generation"] == s["generation"]
                and plan["anchorDigest"] == s["anchorDigest"], "frozen_repair_plan_missing")
        if target_role is not None:
            require(plan["targetRole"] == target_role and plan["targetPvcUid"] == target_pvc,
                    "frozen_repair_target_mismatch")


def cutover_status(s):
    f = s.get("writerFence")
    repair = s.get("repair")
    blockers = []
    if not f or f["phase"] != "frozen":
        blockers.append("writer_stop_evidence")
    if not repair or repair["state"] != "staged_verified":
        blockers.append("verified_staging")
    # These are deliberately unconditional until actually implemented.
    blockers += ["independent_retention", "external_rollback_watermark",
                 "verified_target_mount_and_worktree", "replacement_writer_enrollment",
                 "provider_fence_and_cutover_adapter"]
    return {"tenant": s["tenant"], "generation": s["generation"],
            "epoch": f["epoch"] if f else None, "blockers": blockers,
            "activationAllowed": False}
