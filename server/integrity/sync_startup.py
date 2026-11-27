#!/usr/bin/env python3
"""Opt-in fail-closed preflight for an enrolled Sync writer.

Run under the existing PID1 lease supervisor BEFORE any bootstrap, Git
commit or Syncthing serve. Host-provided startup.json is trusted only on
the same root-owned read-only mount as the witness permit. This checks a
fully verified offline snapshot, not an arbitrary dirty restart. No writes.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import xml.etree.ElementTree as ET

from recovery_crypto import RecoveryError, read, decode, digest, verify as verify_signature
from writer_guard import Guard
from git_integrity import IntegrityError, git, refs, OID
from worktree_integrity import verify
from sync_identity import DEVICE_ID, validate_config, verify_device, require

TENANT = re.compile(r"gtm-[a-f0-9]{32}\Z")
UID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\Z")
DEVICE = re.compile(r"[1-9][0-9]*:[0-9]+\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
REQUIRED = {"refs/heads/"+x for x in ("main","gtm-tenant","gtm-vault","gtm-mail")}
FILES = {"config.xml","cert.pem","key.pem"}
MAX_IDENTITY = 4*1024*1024


def configuration(value):
    require(isinstance(value,dict) and set(value)=={
        "schema","tenant","role","identity","epoch","mounts","refs","anchor","deviceId","folderId","identitySha256"},
        "invalid_startup_inventory")
    require(type(value["schema"]) is int and value["schema"]==1 and value["role"]=="sync"
            and isinstance(value["tenant"],str) and TENANT.fullmatch(value["tenant"])
            and type(value["epoch"]) is int and value["epoch"]>0, "invalid_startup_identity")
    ids=value["identity"]
    require(isinstance(ids,dict) and set(ids)=={"podUid","nodeUid","pvcUids"}
            and isinstance(ids["pvcUids"],list) and len(ids["pvcUids"])==2
            and all(isinstance(x,str) and UID.fullmatch(x) for x in
                    [ids["podUid"],ids["nodeUid"],*ids["pvcUids"]])
            and ids["pvcUids"]==sorted(set(ids["pvcUids"])), "invalid_startup_writer_identity")
    mounts=value["mounts"]
    require(isinstance(mounts,dict) and set(mounts)=={"data","files"}
            and all(isinstance(x,str) and DEVICE.fullmatch(x) for x in mounts.values())
            and mounts["data"]!=mounts["files"], "invalid_startup_mounts")
    expected=value["refs"]
    require(isinstance(expected,dict) and REQUIRED<=set(expected)
            and all(isinstance(k,str) and k.startswith("refs/") and not k.startswith("refs/replace/")
                    and isinstance(v,str) and OID.fullmatch(v) for k,v in expected.items())
            and len({len(v) for v in expected.values()})==1, "invalid_startup_refs")
    require(isinstance(value["deviceId"],str) and DEVICE_ID.fullmatch(value["deviceId"])
            and isinstance(value["folderId"],str) and 0<len(value["folderId"])<=128,
            "invalid_startup_syncthing_identity")
    hashes=value["identitySha256"]
    require(isinstance(hashes,dict) and set(hashes)==FILES and all(
        isinstance(v,str) and HASH.fullmatch(v) for v in hashes.values()), "invalid_startup_hashes")
    return value


def unescape(s):
    return re.sub(r"\\(040|011|012|134)",lambda m:chr(int(m.group(1),8)),s)


def mount_entries(raw):
    rows=[]
    for line in raw.splitlines():
        left,right=line.split(" - ",1)
        a,b=left.split(),right.split()
        require(len(a)>=6 and len(b)==3,"invalid_startup_mountinfo")
        rows.append({"device":a[2],"root":unescape(a[3]),"path":unescape(a[4]),
                     "options":a[5].split(","),"super":b[2].split(","),"filesystem":b[0]})
    return rows


def check_mount(path, entries, device=None, readonly=False):
    path=Path(path)
    require(path.is_absolute() and path.resolve(strict=True)==path and path.is_dir(),
            "invalid_startup_mount_path")
    matches=[m for m in entries if m["path"]==str(path)]
    require(len(matches)==1,"missing_or_stacked_startup_mount")
    m=matches[0]
    st=path.stat()
    actual=str(os.major(st.st_dev))+":"+str(os.minor(st.st_dev))
    require(m["device"]==actual,"startup_mount_device_mismatch")
    if readonly:
        require("ro" in m["options"] and os.statvfs(path).f_flag&os.ST_RDONLY,
                "startup_trust_mount_not_read_only")
    else:
        require(actual==device and m["root"]=="/" and m["filesystem"] in ("ext4","xfs")
                and "rw" in m["options"] and "rw" in m["super"]
                and not os.statvfs(path).f_flag&os.ST_RDONLY, "startup_data_mount_mismatch")
    return m


def trusted_file(path):
    path=Path(path)
    s=path.lstat()
    require(stat.S_ISREG(s.st_mode) and s.st_uid==0 and not s.st_mode&0o022,
            "untrusted_startup_file")
    return path


def identity_bytes(home):
    values={}
    for name in FILES:
        path=home/name
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        with os.fdopen(fd,"rb") as stream:
            s=os.fstat(stream.fileno())
            require(stat.S_ISREG(s.st_mode) and s.st_nlink==1 and 0<s.st_size<=MAX_IDENTITY
                    and not s.st_mode&0o022 and (name!="key.pem" or not s.st_mode&0o077),
                    "invalid_startup_identity_file")
            data=stream.read(MAX_IDENTITY+1)
            require(len(data)==s.st_size,"startup_identity_changed")
            values[name]=data
    require(sum(map(len,values.values()))<=MAX_IDENTITY,"startup_identity_too_large")
    return values


def check_identity(home,cfg):
    values=identity_bytes(home)
    require({n:hashlib.sha256(v).hexdigest() for n,v in values.items()}==cfg["identitySha256"],
            "startup_identity_digest_mismatch")
    validate_config(values["config.xml"],cfg)
    verify_device(home,cfg)


def preflight(root,trust,tenant,identity,*,timeout=86400,mountinfo="/proc/self/mountinfo"):
    root,trust=Path(root),Path(trust)
    started=time.monotonic()
    def remaining():
        budget=timeout-(time.monotonic()-started)
        require(budget>0,"startup_timeout")
        return budget
    def read_mounts():
        return mount_entries(Path(mountinfo).read_text())
    entries=read_mounts()
    check_mount(trust,entries,readonly=True)
    startup=trusted_file(trust/"startup.json")
    cfg=configuration(decode(read(startup)))
    require(cfg["tenant"]==tenant and cfg["identity"]==identity,"startup_enrollment_mismatch")
    def check_permit():
        key=trusted_file(trust/"witness.pub")
        anchor=verify_signature(cfg["anchor"],key)
        require(isinstance(anchor,dict) and anchor.get("schema")==1 and anchor.get("kind")=="generation"
                and anchor.get("tenant")==tenant and type(anchor.get("generation")) is int
                and anchor["generation"]>0 and anchor.get("refsDigest")==digest(cfg["refs"]),
                "startup_anchor_mismatch")
        permit=trusted_file(trust/"permit.json")
        guard=Guard(tenant,"sync",identity,key)
        guard.check(decode(read(permit)))
        require(guard.epoch==cfg["epoch"],"startup_epoch_mismatch")
    def check_paths(entries):
        check_mount(root,entries,cfg["mounts"]["data"])
        check_mount(root/"files",entries,cfg["mounts"]["files"])
        require(not any(m["path"].startswith(str(root)+"/") and m["path"]!=str(root/"files")
                        for m in entries),"unexpected_nested_startup_mount")
        for name in ("git","syncthing"):
            p=root/name
            require(p.is_dir() and p.resolve(strict=True)==p and p.stat().st_dev==root.stat().st_dev,
                    "startup_state_path_mismatch")
    check_permit()
    check_paths(entries)
    check_identity(root/"syncthing",cfg)
    repo=root/"git"
    def check_refs():
        require(git(repo,"symbolic-ref","HEAD",capture=True,timeout=min(60,remaining())).strip()
                =="refs/heads/main","startup_head_not_main")
        require(refs(repo,min(60,remaining()))==cfg["refs"],"startup_refs_mismatch")
    check_refs()
    # One read-only scan per start, no periodic additional job. Unknown files,
    # uncommitted changes or unexpected technical files block rather than
    # silently deleting/committing them. Full ordinary restart policy is open.
    result=verify(repo,root/"files",cfg["refs"]["refs/heads/main"],remaining())
    check_refs()
    entries=read_mounts()
    check_mount(trust,entries,readonly=True)
    check_paths(entries)
    require(configuration(decode(read(trusted_file(startup))))==cfg,"startup_inventory_changed")
    check_identity(root/"syncthing",cfg)
    check_permit()
    return {"state":"startup_preflight_verified","files":result["files"],
            "activationAuthorizedByPreflight":False}


def main():
    try:
        require(os.environ.get("GTM_WRITER_FENCE_MODE")=="required","startup_requires_writer_guard")
        require(os.environ.get("GTM_GIT_LFS_ENABLED","false")=="false"
                and os.environ.get("GTM_RECOVERY_LAYOUT")=="git-only","startup_requires_canonical_git")
        identity={"podUid":os.environ["GTM_FENCE_POD_UID"],
                  "nodeUid":os.environ["GTM_FENCE_NODE_UID"],
                  "pvcUids":sorted(os.environ["GTM_FENCE_PVC_UIDS"].split(","))}
        print(json.dumps(preflight("/data","/run/gtmd-writer-fence",
                                   os.environ["GTM_FENCE_TENANT"],identity)),flush=True)
    except (RecoveryError,IntegrityError,OSError,ValueError,KeyError,TypeError,AttributeError,ET.ParseError) as exc:
        print(json.dumps({"event":"gtmd_sync_startup","state":"blocked",
                          "reason":str(exc) if isinstance(exc,(RecoveryError,IntegrityError)) else type(exc).__name__}),
              flush=True)
        return 1
    return 0


if __name__=="__main__":
    raise SystemExit(main())
