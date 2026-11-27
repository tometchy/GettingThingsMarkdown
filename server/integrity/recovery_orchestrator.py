#!/usr/bin/env python3
"""Idempotent witness-driven repair orchestration, no live Kubernetes writes.

External trusted agents supply stop proofs and source evidence. The
allocator supplies a fresh verified target PVC UID. No operator can use this
program to activate an unverified replica or bypass independent retention.
"""
import argparse
import json
from pathlib import Path

from recovery_crypto import read, decode, RecoveryError
from recovery_witness import require, TENANT, identifier, ROLES
from witness_client import Client


def reconcile(api, tenant, incident, target_role, target_pvc, *, evidence=None, stop_proofs=None):
    require(TENANT.fullmatch(tenant) and identifier(incident) and target_role in ROLES
            and identifier(target_pvc), "invalid_incident")
    def call(action, **fields):
        return api.call({"tenant":tenant,"action":action,**fields})
    s=call("status")
    require(s.get("writerFence"), "writer_fencing_not_enrolled")
    # Initial mutation writes target identity and freezes in one witness CAS.
    # Every later retry must match the immutable intent.
    if s["writerFence"]["phase"]=="open":
        call("plan-repair",incident=incident,targetRole=target_role,targetPvcUid=target_pvc)
        s=call("status")
    freeze=s["writerFence"]["freeze"]
    plan=freeze.get("plan")
    require(freeze["incident"]==incident and plan and plan["targetRole"]==target_role
            and plan["targetPvcUid"]==target_pvc and plan["generation"]==s["generation"]
            and plan["anchorDigest"]==s["anchorDigest"], "repair_intent_mismatch")
    if s["writerFence"]["phase"]=="freezing":
        for role, proof in (stop_proofs or {}).items():
            if role not in freeze["proofs"]:
                call("confirm-writer-stop",role=role,evidence=proof)
        s=call("status")
        if s["writerFence"]["phase"]!="frozen":
            return {"state":"awaiting_writer_stop","incident":incident,
                    "missingRoles":[r for r in ROLES if r not in s["writerFence"]["freeze"]["proofs"]],
                    "notBeforeUnix":freeze["challenge"]["notBeforeUnix"],"activationAllowed":False}
    require(s["writerFence"]["phase"]=="frozen","unexpected_writer_phase")
    repair=s.get("repair")
    if repair is None:
        ch=s.get("challenge")
        # An expired challenge is superseded by witness, never by local clock.
        # If a request outcome was lost, re-read status on the next cycle.
        if ch is None:
            challenge=call("challenge",kind="repair",targetRole=target_role,targetPvcUid=target_pvc)
        else:
            require(ch["kind"]=="repair" and ch["targetRole"]==target_role
                    and ch["targetPvcUid"]==target_pvc,"repair_challenge_mismatch")
            try:
                challenge=call("resume-challenge")
            except RecoveryError:
                # Server refuses a new challenge until the old one has expired.
                challenge=call("challenge",kind="repair",targetRole=target_role,targetPvcUid=target_pvc)
        if evidence is None:
            return {"state":"awaiting_source_evidence","incident":incident,"challenge":challenge,
                    "sourceRole":"history" if target_role=="sync" else "sync","activationAllowed":False}
        call("start-repair",evidence=evidence)
        s=call("status")
        repair=s["repair"]
    require(repair["targetRole"]==target_role and repair["targetPvcUid"]==target_pvc
            and repair["generation"]==s["generation"],"repair_operation_mismatch")
    if repair["state"]=="staged_verified":
        return {"state":"staged_verified","incident":incident,"operation":repair["operation"],
                "cutover":call("cutover-status"),"activationAllowed":False}
    require(repair["state"] in ("authorized","ticket_issued"),"unexpected_repair_state")
    # Stage runner retrieves the signed ticket itself. Do not expose the
    # complete ref manifest or source URL in ordinary operational logs.
    call("ticket",operation=repair["operation"])
    return {"state":"awaiting_staging_worker","incident":incident,"operation":repair["operation"],
            "targetPvcUid":target_pvc,"generation":s["generation"],"activationAllowed":False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for field in ("endpoint","ca","cert","tls-key","tenant","incident","target-role","target-pvc"):
        p.add_argument("--"+field,required=True)
    p.add_argument("--source-evidence")
    p.add_argument("--sync-stop")
    p.add_argument("--history-stop")
    p.add_argument("--challenge-output")
    a=p.parse_args()
    try:
        def load(path):
            return decode(read(path)) if path else None
        proofs={r:load(v) for r,v in (("sync",a.sync_stop),("history",a.history_stop)) if v}
        result=reconcile(Client(a.endpoint,a.ca,a.cert,a.tls_key),a.tenant,a.incident,a.target_role,
                          a.target_pvc,evidence=load(a.source_evidence),stop_proofs=proofs)
        challenge=result.pop("challenge",None)
        if challenge is not None and a.challenge_output:
            # The challenge contains refs, keep it out of logs and private.
            from git_integrity import atomic_json
            atomic_json(Path(a.challenge_output),challenge)
        print(json.dumps(result,separators=(",",":")))
    except (RecoveryError,OSError,ValueError,KeyError,TypeError) as exc:
        print(json.dumps({"event":"gtmd_recovery_orchestrator","state":"blocked",
            "reason":str(exc) if isinstance(exc,RecoveryError) else type(exc).__name__}))
        return 1
    return 0


if __name__=="__main__":
    raise SystemExit(main())
