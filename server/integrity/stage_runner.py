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
import re
from pathlib import Path
import stat
import subprocess
import time
import tempfile
import tarfile
import xml.etree.ElementTree as ET

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


def validate_contents(root, operation):
    # Fresh filesystem or attempts from this exact witness operation only.
    # Never delete, reuse or overwrite other content.
    for entry in root.iterdir():
        if entry.name == "lost+found":
            require(entry.is_dir() and not entry.is_symlink() and not any(entry.iterdir()),
                    "unexpected_staging_content")
        elif entry.name == ".gtmd-stage.lock":
            require(entry.is_file() and not entry.is_symlink(), "unexpected_staging_content")
        else:
            require(re.fullmatch(re.escape(operation) + r"-[1-9][0-9]*", entry.name)
                    and entry.is_dir() and not entry.is_symlink(), "unexpected_staging_content")


def prepare_recorded_sync(api, identity, operation, root, cfg, age_key, timeout):
    from sync_state import prepare
    require(identity["target_role"] == "sync", "sync_state_requires_sync_role")
    status = api.call({"action":"status","tenant":identity["tenant"]})
    repair = status["repair"]
    require(status["generation"] == identity["generation"] and repair["operation"] == operation
            and repair["targetRole"] == "sync" and repair["targetPvcUid"] == identity["target_pvc_uid"]
            and repair["state"] == "staged_verified" and status["writerFence"]["phase"] == "frozen",
            "sync_state_repair_not_verified")
    ticket, receipt = repair["ticket"], repair["receipt"]
    require(ticket["tenant"] == identity["tenant"] and ticket["generation"] == identity["generation"]
            and ticket["targetPvcUid"] == identity["target_pvc_uid"] and ticket["targetRole"] == "sync"
            and ticket["targetCloud"] == identity["target_cloud"]
            and re.fullmatch(re.escape(operation)+r"-[1-9][0-9]*", ticket["operation"])
            and receipt["ticketSha256"] == hashlib.sha256(canonical(ticket)).hexdigest()
            and receipt["operation"] == ticket["operation"] and receipt["physicalScrub"] is True
            and receipt["activationAllowed"] is False and receipt["state"] == "staged_verified",
            "sync_state_receipt_mismatch")
    attempt = root/ticket["operation"]
    require(attempt.is_dir() and not attempt.is_symlink()
            and (attempt/"repository.git").is_dir() and not (attempt/"repository.git").is_symlink()
            and (attempt/"receipt.json").is_file() and not (attempt/"receipt.json").is_symlink()
            and decode(read(attempt/"receipt.json")) == receipt, "sync_state_local_receipt_mismatch")
    return prepare(attempt/"repository.git", attempt/"sync-state", ticket["refs"], cfg, age_key, timeout)


def run(api, identity, operation, root, witness_public, target_key, timeout=86400,
        allow_local_test_source=False, guard=None, sync_config=None, sync_age_key=None):
    require(identifier(operation) and TENANT.fullmatch(identity["tenant"]), "invalid_operation")
    root = Path(root)
    require((sync_config is None and sync_age_key is None) or (sync_config is not None and sync_age_key is not None
            and identity["target_role"] == "sync"), "invalid_sync_state_request")
    require(guard is not None or allow_local_test_source, "verified_staging_binding_required")
    # Hold the mount open throughout the job: ordinary unmount is busy.
    # No writes before the first real binding check. A privileged lazy
    # unmount remains outside the trusted-host guarantee.
    root_fd = os.open(root, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    fd = None
    def check():
        if guard is not None:
            observed = guard()
            require(Path(observed["mount"]) == root, "staging_mount_path_mismatch")
            validate_mount(root, observed["device"])
        pinned = os.fstat(root_fd)
        current = root.stat(follow_symlinks=False)
        require((pinned.st_dev, pinned.st_ino) == (current.st_dev, current.st_ino),
                "staging_mount_changed")
    try:
        check()
        # Lock relative to the pinned directory, not an untrusted path.
        fd = os.open(".gtmd-stage.lock", os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,
                     0o600, dir_fd=root_fd)
        lock = os.fstat(fd)
        require(stat.S_ISREG(lock.st_mode) and lock.st_uid == os.getuid()
                and not lock.st_mode & 0o077, "insecure_stage_lock")
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        validate_contents(root, operation)
        pair = get_ticket(api,identity,operation)
        if pair is None:
            if sync_config is not None:
                check()
                result = prepare_recorded_sync(api, identity, operation, root, sync_config, sync_age_key, timeout)
                check()
                return result
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
        check()
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
        check()
        return api.call({"action":"receipt","tenant":identity["tenant"],
                         "operation":operation,"evidence":sign(receipt,target_key)})
    finally:
        if fd is not None:
            os.close(fd)
        os.close(root_fd)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for k in ("endpoint","ca","cert","tls-key","tenant","target-role","target-cloud",
              "target-pvc-uid","operation","witness-public","target-key",
              "binding-inventory","storage-inventory","repair-class","kube-endpoint","kube-ca","kube-token"):
        p.add_argument("--"+k,required=True)
    p.add_argument("--sync-inventory", help="Optional one-shot offline Sync state after recorded Git receipt")
    p.add_argument("--sync-age-key", help="One-shot infrastructure identity decryption key")
    p.add_argument("--generation",type=int,required=True)
    p.add_argument("--timeout",type=int,default=86400)
    a=p.parse_args()
    try:
        from stage_mount_guard import configuration, verify_binding
        from controller import inventory
        from local_lvm_fence import Lvm
        from kube_client import Kube, ApiError
        require(os.geteuid() == 0, "trusted_host_root_required")
        for path in (a.binding_inventory, a.storage_inventory, a.witness_public, a.target_key,
                     a.ca, a.cert, a.tls_key, a.kube_ca, a.kube_token):
            st = Path(path).lstat()
            require(stat.S_ISREG(st.st_mode) and st.st_uid == 0 and not st.st_mode & 0o022,
                    "host_trust_file_not_root_owned")
        for path in (a.target_key, a.tls_key, a.kube_token):
            require(not Path(path).stat().st_mode & 0o077, "insecure_host_credential")
        host = configuration(decode(read(a.binding_inventory)))
        storage = inventory(decode(read(a.storage_inventory)))
        identity={k:getattr(a,k) for k in ("tenant","generation","target_role","target_cloud","target_pvc_uid")}
        witness = Client(a.endpoint,a.ca,a.cert,a.tls_key)
        kube = Kube(a.kube_endpoint,a.kube_ca,a.kube_token)
        lvm = Lvm()
        machine = Path("/etc/machine-id").read_text().strip()
        def guard():
            try:
                return verify_binding(kube,witness,a.witness_public,storage,a.repair_class,
                                      host,identity,a.operation,lvm,machine)
            except ApiError as exc:
                raise RecoveryError("staging_kubernetes_http_" + str(exc.code)) from None
        require(bool(a.sync_inventory) == bool(a.sync_age_key), "sync_inventory_and_key_required")
        sync_config = None
        if a.sync_inventory:
            for path in (a.sync_inventory,a.sync_age_key):
                st = Path(path).lstat()
                require(stat.S_ISREG(st.st_mode) and st.st_uid == 0 and not st.st_mode & 0o077,
                        "insecure_sync_restore_file")
            sync_config = decode(read(a.sync_inventory))
        result=run(witness,identity,a.operation,Path(host["mount"]),
                   a.witness_public,a.target_key,a.timeout,guard=guard,
                   sync_config=sync_config,sync_age_key=a.sync_age_key)
        print(json.dumps(result,separators=(",",":")))
    except (RecoveryError,IntegrityError,OSError,ValueError,KeyError,TypeError,AttributeError,ImportError,subprocess.SubprocessError,tarfile.TarError,ET.ParseError,EOFError) as exc:
        print(json.dumps({"event":"gtmd_stage_runner","state":"failed",
            "reason":str(exc) if isinstance(exc,(RecoveryError,IntegrityError)) else type(exc).__name__}))
        return 1
    return 0


if __name__=="__main__":
    raise SystemExit(main())
