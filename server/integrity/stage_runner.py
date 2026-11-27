#!/usr/bin/env python3
"""Trusted target-side repair staging runner with idempotent receipt submission.

Runs outside tenant workloads on a dedicated provisioned staging mount.
It never changes Kubernetes objects or activates a repo. Expected identity
must be independently provided by the trusted allocator/host configuration.
"""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import tempfile

from authorized_rebuild import verify_ticket, execute
from git_integrity import IntegrityError, atomic_json
from physical_scrub import scrub
from recovery_crypto import RecoveryError, read, decode, sign, canonical
from recovery_witness import require, identifier, TENANT
from witness_client import Client


def validate_mount(root, expected_dev):
    path = Path(root)
    require(path.is_absolute() and path.resolve(strict=True) == path
            and path.is_dir() and os.path.ismount(path), "staging_mount_not_verified")
    st = path.stat()
    require(str(os.major(st.st_dev)) + ":" + str(os.minor(st.st_dev)) == expected_dev,
            "staging_device_mismatch")
    require(st.st_uid == os.getuid() and st.st_mode & 0o022 == 0,
            "insecure_staging_mount")
    return path


def get_ticket(api, identity, operation):
    status = api.call({"action":"status","tenant":identity["tenant"]})
    repair = status.get("repair")
    require(repair and repair["operation"] == operation
            and status["generation"] == identity["generation"]
            and repair["targetRole"] == identity["target_role"]
            and repair["targetPvcUid"] == identity["target_pvc_uid"],
            "witness_target_or_generation_mismatch")
    if repair["state"] == "staged_verified":
        return None
    result = api.call({"action":"ticket","tenant":identity["tenant"],"operation":operation})
    require(result.get("activationAllowed") is False, "unexpected_activation")
    return base64.b64decode(result["ticket"],validate=True), base64.b64decode(result["signature"],validate=True)


def run(api, identity, operation, root, witness_public, target_key, timeout=86400,
        allow_local_test_source=False):
    require(identifier(operation) and TENANT.fullmatch(identity["tenant"]), "invalid_operation")
    root = Path(root)
    # Host lock protects staging against concurrent runners on one mount.
    fd = os.open(root/".gtmd-stage.lock", os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        pair = get_ticket(api,identity,operation)
        if pair is None:
            return {"state":"already_recorded","activationAllowed":False}
        raw,sig = pair
        with tempfile.TemporaryDirectory() as d:
            ticket_path, sig_path = Path(d)/"ticket",Path(d)/"signature"
            ticket_path.write_bytes(raw)
            sig_path.write_bytes(sig)
            ticket,digest = verify_ticket(ticket_path,sig_path,witness_public,
                    **identity,allow_local_test_source=allow_local_test_source,
                    allow_expired_for_receipt=True)
        require(ticket["operation"].startswith(operation+"-"),"ticket_operation_mismatch")
        destination=root/ticket["operation"]
        receipt_path=destination/"receipt.json"
        if destination.exists() or destination.is_symlink():
            require(destination.is_dir() and not destination.is_symlink()
                    and receipt_path.is_file() and not receipt_path.is_symlink(),
                    "incomplete_staging_requires_new_attempt")
            receipt=decode(read(receipt_path))
            require(receipt.get("ticketSha256")==digest
                    and receipt.get("operation")==ticket["operation"]
                    and receipt.get("tenant")==ticket["tenant"]
                    and receipt.get("targetPvcUid")==ticket["targetPvcUid"]
                    and receipt.get("generation")==ticket["generation"]
                    and receipt.get("activationAllowed") is False
                    and receipt.get("physicalScrub") is True,
                    "existing_receipt_mismatch")
            # Do not blindly sign an existing receipt after restart. Re-read
            # and verify the staged contents against the authenticated ticket.
            scrub(destination/"repository.git",timeout,ticket["refs"])
        else:
            require(time.time() < ticket["expiresUnix"], "ticket_expired_before_execution")
            receipt=execute(ticket,digest,destination,timeout,physical=True)
        require(receipt.get("state")=="staged_verified","invalid_receipt")
        return api.call({"action":"receipt","tenant":identity["tenant"],
                         "operation":operation,"evidence":sign(receipt,target_key)})
    finally:
        os.close(fd)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for k in ("endpoint","ca","cert","tls-key","tenant","target-role","target-cloud",
              "target-pvc-uid","operation","mount","mount-dev","witness-public","target-key"):
        p.add_argument("--"+k,required=True)
    p.add_argument("--generation",type=int,required=True)
    p.add_argument("--timeout",type=int,default=86400)
    a=p.parse_args()
    try:
        root=validate_mount(a.mount,a.mount_dev)
        identity={k:getattr(a,k) for k in ("tenant","generation","target_role","target_cloud","target_pvc_uid")}
        result=run(Client(a.endpoint,a.ca,a.cert,a.tls_key),identity,a.operation,root,
                   a.witness_public,a.target_key,a.timeout)
        print(json.dumps(result,separators=(",",":")))
    except (RecoveryError,IntegrityError,OSError,ValueError,KeyError,TypeError) as exc:
        print(json.dumps({"event":"gtmd_stage_runner","state":"failed",
            "reason":str(exc) if isinstance(exc,(RecoveryError,IntegrityError)) else type(exc).__name__}))
        return 1
    return 0


if __name__=="__main__":
    raise SystemExit(main())
