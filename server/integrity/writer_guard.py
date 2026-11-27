#!/usr/bin/env python3
"""Fail-closed PID 1 supervisor for an explicitly enrolled data container.

The permit file is published by a trusted external agent on a read-only
mount. No witness/replica private keys in the data container. Exiting PID 1
terminates the container PID namespace; shared host PID namespaces forbidden.
This is cooperative process fencing, not a physical disk/provider fence.
"""
import argparse
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from recovery_crypto import RecoveryError, read, decode, verify, digest

MAX_TTL = 60
SKEW = 5


class Guard:
    def __init__(self, tenant, role, identity, key, wall=time.time, mono=time.monotonic):
        self.tenant, self.role, self.identity, self.key = tenant, role, identity, key
        self.wall, self.mono = wall, mono
        self.epoch = self.sequence = self.last_digest = None
        self.deadline = 0

    def check(self, envelope):
        data = verify(envelope, self.key)
        if (not isinstance(data, dict) or set(data) != {"schema","kind","tenant","role","epoch",
                    "identity","sequence","issuedUnix","expiresUnix"}
                or data["schema"] != 1 or data["kind"] != "writer_permit"
                or data["tenant"] != self.tenant or data["role"] != self.role
                or data["identity"] != self.identity):
            raise RecoveryError("writer_permit_identity_mismatch")
        epoch, seq = data["epoch"], data["sequence"]
        issued, expires = data["issuedUnix"], data["expiresUnix"]
        wall, mono = self.wall(), self.mono()
        if (any(type(x) is not int for x in (epoch,seq,issued,expires))
                or epoch <= 0 or seq <= 0 or not 0 < expires-issued <= MAX_TTL
                or issued > wall+SKEW or wall >= expires-SKEW):
            raise RecoveryError("writer_permit_expired_or_invalid")
        # A running process never transitions to another epoch.
        if self.epoch is not None and (epoch != self.epoch or seq < self.sequence):
            raise RecoveryError("writer_permit_rollback_or_epoch_change")
        token = digest(data)
        if seq == self.sequence:
            if token != self.last_digest:
                raise RecoveryError("writer_permit_sequence_conflict")
        else:
            # Monotonic deadline prevents an old token gaining extra life
            # from backward wall-clock changes or repeated reads.
            self.deadline = mono + min(MAX_TTL-SKEW, expires-SKEW-wall)
            self.epoch, self.sequence, self.last_digest = epoch, seq, token
        if mono >= self.deadline:
            raise RecoveryError("writer_permit_monotonic_expiry")
        return self.deadline


def terminate(child):
    if child is None:
        return
    # Kill the entire child process group even if its original leader exited.
    # PID1 exit additionally kills descendants in the container PID namespace.
    try:
        os.killpg(child.pid,signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def supervise(guard, permit, command, poll=1, require_pid1=True):
    if require_pid1 and os.getpid() != 1:
        raise RecoveryError("writer_guard_must_be_pid1")
    child = None
    stop = False
    def handler(*_):
        nonlocal stop
        stop = True
    old = {sig:signal.signal(sig,handler) for sig in (signal.SIGTERM,signal.SIGINT)}
    try:
        guard.check(decode(read(permit)))
        if stop:
            return 1
        child = subprocess.Popen(command,start_new_session=True)
        while not stop:
            if child.poll() is not None:
                return child.returncode
            guard.check(decode(read(permit)))
            time.sleep(poll)
        return 1
    except (RecoveryError,OSError,ValueError,TypeError) as exc:
        print(json.dumps({"event":"gtmd_writer_guard","state":"fenced",
             "reason":str(exc) if isinstance(exc,RecoveryError) else type(exc).__name__}),flush=True)
        return 1
    finally:
        terminate(child)
        for sig,h in old.items():
            signal.signal(sig,h)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--role",choices=("sync","history"),required=True)
    p.add_argument("command",nargs=argparse.REMAINDER)
    a=p.parse_args()
    command=a.command[1:] if a.command[:1]==["--"] else a.command
    # Legacy development explicitly remains unfenced. Production must enforce
    # opt-in at admission/provisioning; never claim fencing for this mode.
    mode=os.environ.get("GTM_WRITER_FENCE_MODE","development")
    if mode=="development":
        init="/sbin/tini" if a.role=="sync" else "/usr/bin/dumb-init"
        os.execv(init,[init,"--",*command])
    if mode!="required":
        raise RecoveryError("invalid_writer_fence_mode")
    tenant=os.environ["GTM_FENCE_TENANT"]
    identity={k:os.environ[v] for k,v in (("podUid","GTM_FENCE_POD_UID"),
              ("pvcUid","GTM_FENCE_PVC_UID"),("nodeUid","GTM_FENCE_NODE_UID"))}
    # Immutable trust material, separate from the tenant PVC.
    root=Path("/run/gtmd-writer-fence")
    return supervise(Guard(tenant,a.role,identity,root/"witness.pub"),
                     root/"permit.json",command)


if __name__=="__main__":
    try:
        raise SystemExit(main())
    except (RecoveryError,OSError,KeyError,ValueError) as exc:
        print(json.dumps({"event":"gtmd_writer_guard","state":"fenced",
                          "reason":type(exc).__name__}),flush=True)
        raise SystemExit(1)
