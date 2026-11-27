#!/usr/bin/env python3
"""Trusted host-side one-shot permit publisher; never mount private keys in pods."""
import argparse
import json
import os
from pathlib import Path
import secrets
import tempfile
import time

from recovery_crypto import sign, verify, RecoveryError
from recovery_witness import require
from witness_client import Client


def renew(api, tenant, role, identity, epoch, key, public, output, now=None):
    now=int(time.time()) if now is None else now
    request={"schema":1,"kind":"writer_permit_request","tenant":tenant,"role":role,
             "epoch":epoch,"identity":identity,"requestId":secrets.token_hex(24),"issuedUnix":now}
    result=api.call({"tenant":tenant,"action":"writer-permit","role":role,"evidence":sign(request,key)})
    value=verify(result,public)
    require(value.get("kind")=="writer_permit" and value.get("tenant")==tenant
            and value.get("role")==role and value.get("identity")==identity and value.get("epoch")==epoch,
            "returned_permit_identity_mismatch")
    # The host-owned directory must be mounted read-only into the data pod;
    # permit is not a secret, but role and witness private keys are.
    output=Path(output)
    require(output.parent.is_dir() and not output.parent.is_symlink(), "invalid_permit_directory")
    # Set readable permissions before atomic publication; no transient 0600
    # window that would unnecessarily fence the unprivileged data process.
    with tempfile.NamedTemporaryFile(mode="w",dir=output.parent,delete=False) as tmp:
        name=tmp.name
        try:
            os.fchmod(tmp.fileno(),0o644)
            json.dump(result,tmp,separators=(",",":"))
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
    try:
        os.replace(name,output)
    finally:
        Path(name).unlink(missing_ok=True)
    return {"state":"permit_published","expiresUnix":value["expiresUnix"]}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for k in ("endpoint","ca","cert","tls-key","tenant","role","pod-uid","pvc-uid","node-uid",
              "replica-key","witness-public","output"):
        p.add_argument("--"+k,required=True)
    p.add_argument("--epoch",type=int,required=True)
    a=p.parse_args()
    try:
        result=renew(Client(a.endpoint,a.ca,a.cert,a.tls_key),a.tenant,a.role,
            {"podUid":a.pod_uid,"pvcUid":a.pvc_uid,"nodeUid":a.node_uid},a.epoch,
            a.replica_key,a.witness_public,a.output)
        print(json.dumps(result))
    except (RecoveryError,OSError,ValueError,TypeError) as exc:
        print(json.dumps({"event":"gtmd_writer_permit_agent","state":"renewal_failed",
                         "reason":type(exc).__name__}))
        return 1
    return 0


if __name__=="__main__":
    raise SystemExit(main())
