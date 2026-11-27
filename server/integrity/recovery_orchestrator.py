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
    # An emergency freeze may precede allocation. Attach the target once;
    # every later retry must match the immutable incident and intent.
    if s["writerFence"]["phase"]=="open":
        call("plan-repair",incident=incident,targetRole=target_role,targetPvcUid=target_pvc)
        s=call("status")
    freeze=s["writerFence"]["freeze"]
    if freeze.get("plan") is None:
        require(freeze["incident"]==incident,"repair_intent_mismatch")
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
                    "notBeforeUnix":freeze["challenge"]["notBeforeUnix"],
                    "freezeChallenge":call("resume-freeze"),"activationAllowed":False}
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


def reconcile_with_allocator(api, tenant, incident, target_role, allocate, *,
                             evidence=None, stop_proofs=None):
    """One invocation of the existing orchestrator, no new worker or DB."""
    allocation = allocate()
    require(allocation.get("activationAllowed") is False
            and allocation.get("tenant") == tenant and allocation.get("incident") == incident
            and allocation.get("role") == target_role, "allocation_identity_mismatch")
    if allocation["state"] == "allocated":
        result = reconcile(api, tenant, incident, target_role, allocation["targetPvcUid"],
                           evidence=evidence, stop_proofs=stop_proofs)
        return {**result, "allocation": allocation}
    require(allocation["state"] in ("waiting_for_capacity", "waiting_for_binding",
                                    "waiting_for_anchor"), "allocation_not_ready")
    # Stop evidence can arrive before capacity. Preserve it in the witness
    # rather than waiting or adding another incident record.
    def call(action, **fields):
        return api.call({"tenant": tenant, "action": action, **fields})
    status = call("status")
    freeze = status["writerFence"]["freeze"]
    require(freeze["incident"] == incident, "repair_intent_mismatch")
    for role, proof in (stop_proofs or {}).items():
        if role not in freeze["proofs"]:
            call("confirm-writer-stop", role=role, evidence=proof)
    status = call("status")
    freeze = status["writerFence"]["freeze"]
    return {**allocation, "missingRoles": [r for r in ROLES if r not in freeze["proofs"]],
            "notBeforeUnix": freeze["challenge"]["notBeforeUnix"],
            "freezeChallenge": call("resume-freeze"), "activationAllowed": False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for field in ("endpoint","ca","cert","tls-key","tenant","incident","target-role"):
        p.add_argument("--"+field,required=True)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--target-pvc")
    target.add_argument("--allocate-development", action="store_true")
    for field in ("inventory", "repair-class", "kube-endpoint", "kube-ca", "kube-token", "witness-public"):
        p.add_argument("--"+field)
    p.add_argument("--source-evidence")
    p.add_argument("--sync-stop")
    p.add_argument("--history-stop")
    p.add_argument("--challenge-output")
    p.add_argument("--freeze-output")
    a=p.parse_args()
    try:
        def load(path):
            return decode(read(path)) if path else None
        proofs={r:load(v) for r,v in (("sync",a.sync_stop),("history",a.history_stop)) if v}
        api = Client(a.endpoint,a.ca,a.cert,a.tls_key)
        evidence = load(a.source_evidence)
        if a.allocate_development:
            from controller import inventory
            from kube_client import Kube, ApiError
            from repair_allocator import reconcile as allocate
            require(all((a.inventory, a.repair_class, a.kube_endpoint, a.kube_ca,
                         a.kube_token, a.witness_public)), "allocation_configuration_missing")
            kube = Kube(a.kube_endpoint, a.kube_ca, a.kube_token)
            cfg = inventory(load(a.inventory))
            try:
                result = reconcile_with_allocator(api, a.tenant, a.incident, a.target_role,
                    lambda: allocate(kube, api, a.witness_public, cfg, a.repair_class,
                                     a.tenant, a.incident, a.target_role, apply=True),
                    evidence=evidence, stop_proofs=proofs)
            except ApiError as exc:
                raise RecoveryError("allocation_kubernetes_http_" + str(exc.code)) from None
        else:
            result=reconcile(api,a.tenant,a.incident,a.target_role,a.target_pvc,
                             evidence=evidence,stop_proofs=proofs)
        freeze_challenge=result.pop("freezeChallenge",None)
        if freeze_challenge is not None and a.freeze_output:
            from git_integrity import atomic_json
            atomic_json(Path(a.freeze_output),freeze_challenge)
        challenge=result.pop("challenge",None)
        if challenge is not None and a.challenge_output:
            # The challenge contains refs, keep it out of logs and private.
            from git_integrity import atomic_json
            atomic_json(Path(a.challenge_output),challenge)
        print(json.dumps(result,separators=(",",":")))
    except (RecoveryError,OSError,ValueError,KeyError,TypeError,AttributeError,ImportError) as exc:
        print(json.dumps({"event":"gtmd_recovery_orchestrator","state":"blocked",
            "reason":str(exc) if isinstance(exc,RecoveryError) else type(exc).__name__}))
        return 1
    return 0


if __name__=="__main__":
    raise SystemExit(main())
