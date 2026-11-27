#!/usr/bin/env python3
"""One-shot host publication of the startup record for an enrolled Sync pod.

Uses the existing independent inventory, LVM binding verifier and current
witness anchor. No new daemon, database, lease or authorization. Must run
before the workload is allowed to start; no mutation of tenant data.
"""
import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from recovery_crypto import RecoveryError, read, decode, digest, verify
from sync_identity import require
from sync_startup import configuration
from git_integrity import refs, IntegrityError
from local_lvm_fence import Lvm, configuration as lvm_configuration
from lvm_mount_binding import bound_mounts, verify_mount
from kube_client import Kube, ApiError
from witness_client import Client


def current(api, public, cfg):
    s=api.call({"action":"status","tenant":cfg["tenant"]})
    f=s.get("writerFence")
    require(f and f["phase"]=="open" and f["writers"]["sync"]==cfg["identity"]
            and s["repair"] is None and s["generation"]>0, "startup_writer_not_open_or_enrolled")
    anchor=verify(s["anchor"],public)
    require(anchor["schema"]==1 and anchor["kind"]=="generation"
            and anchor["tenant"]==cfg["tenant"] and anchor["generation"]==s["generation"]
            and digest(anchor)==s["anchorDigest"], "startup_current_anchor_mismatch")
    return f["epoch"],s["anchor"],anchor


def build(kube, witness, public, cfg, expected, node, root, machine, lvm,
          mountinfo="/proc/self/mountinfo"):
    cfg=lvm_configuration(cfg)
    require(cfg["role"]=="sync" and machine==cfg["machineId"]
            and len(cfg["identity"]["pvcUids"])==2, "startup_host_or_role_mismatch")
    require(isinstance(expected,dict) and set(expected)=={"deviceId","folderId","identitySha256"},
            "invalid_expected_sync_identity")
    epoch,envelope,anchor=current(witness,public,cfg)
    def sample():
        paths=bound_mounts(kube,cfg,node,root)
        ns="/api/v1/namespaces/"+cfg["tenant"]+"/persistentvolumeclaims/"
        # Resolve names separately from the independently enrolled UID set.
        ids={kind:kube.get(ns+claim)["metadata"]["uid"]
             for kind,claim in (("data","sync-data"),("files","sync-live"))}
        require(set(ids.values())==set(paths) and len(set(ids.values()))==2,
                "startup_claim_mapping_mismatch")
        raw=Path(mountinfo).read_text()
        mounts={kind:verify_mount(paths[uid],lvm.inspect(cfg,uid),raw)["device"]
                for kind,uid in ids.items()}
        repo=paths[ids["data"]]/"git"
        require(repo.is_dir() and not repo.is_symlink()
                and repo.stat().st_dev==paths[ids["data"]].stat().st_dev,
                "startup_repository_path_mismatch")
        manifest=refs(repo)
        require(digest(manifest)==anchor["refsDigest"],"startup_refs_not_current_anchor")
        return paths,ids,mounts,manifest
    first=sample()
    require(first==sample(),"startup_binding_changed")
    require(current(witness,public,cfg)==(epoch,envelope,anchor),"startup_anchor_or_epoch_changed")
    _,_,mounts,manifest=first
    return configuration({"schema":1,"tenant":cfg["tenant"],"role":"sync",
                          "identity":cfg["identity"],"epoch":epoch,"mounts":mounts,
                          "refs":manifest,"anchor":envelope,**expected})


def publish(directory, record):
    directory=Path(directory)
    s=directory.lstat()
    require(directory.is_absolute() and directory.resolve(strict=True)==directory
            and stat.S_ISDIR(s.st_mode) and s.st_uid==0 and not s.st_mode&0o022,
            "untrusted_startup_directory")
    path=directory/"startup.json"
    # One record per enrolled startup directory. A retry may repeat the
    # exact same observation but cannot silently replace a previous record.
    if path.exists() or path.is_symlink():
        st=path.lstat()
        require(stat.S_ISREG(st.st_mode) and st.st_uid==0 and not st.st_mode&0o022
                and decode(read(path))==record,"conflicting_startup_record")
        return
    fd,tmp=tempfile.mkstemp(prefix=".startup-",dir=directory)
    try:
        with os.fdopen(fd,"w") as stream:
            json.dump(record,stream,separators=(",",":"))
            stream.flush()
            os.fchmod(stream.fileno(),0o644)
            os.fsync(stream.fileno())
        # Atomic creation without overwriting a record installed by another
        # host invocation. Retry will check exact identity instead.
        os.link(tmp,path,follow_symlinks=False)
        dfd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        Path(tmp).unlink(missing_ok=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for field in ("inventory","expected-identity","node-name","mount-root","permit-directory",
                  "kube-endpoint","kube-ca","kube-token","witness-endpoint",
                  "witness-ca","witness-cert","witness-key","witness-public"):
        p.add_argument("--"+field,required=True)
    p.add_argument("--apply-development-publish",action="store_true")
    a=p.parse_args()
    try:
        require(os.geteuid()==0,"trusted_host_root_required")
        for path in (a.inventory,a.expected_identity,a.kube_ca,a.kube_token,a.witness_ca,
                     a.witness_cert,a.witness_key,a.witness_public):
            s=Path(path).lstat()
            require(stat.S_ISREG(s.st_mode) and s.st_uid==0 and not s.st_mode&0o022,
                    "host_trust_file_not_root_owned")
        for path in (a.kube_token,a.witness_key):
            require(not Path(path).stat().st_mode&0o077,"insecure_host_credential")
        result=build(Kube(a.kube_endpoint,a.kube_ca,a.kube_token),
                     Client(a.witness_endpoint,a.witness_ca,a.witness_cert,a.witness_key),
                     a.witness_public,decode(read(a.inventory)),decode(read(a.expected_identity)),
                     a.node_name,a.mount_root,Path("/etc/machine-id").read_text().strip(),Lvm())
        if a.apply_development_publish:
            publish(a.permit_directory,result)
        print(json.dumps({"state":"startup_record_published" if a.apply_development_publish
                          else "startup_record_verified","activationAllowed":False}))
    except (RecoveryError,IntegrityError,ApiError,OSError,ValueError,KeyError,TypeError,
            AttributeError,subprocess.SubprocessError) as exc:
        print(json.dumps({"state":"blocked","reason":str(exc) if isinstance(exc,(RecoveryError,IntegrityError))
                          else type(exc).__name__,"activationAllowed":False}))
        return 1
    return 0


if __name__=="__main__":
    raise SystemExit(main())
