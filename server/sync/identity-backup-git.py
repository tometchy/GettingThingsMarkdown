#!/usr/bin/env python3
"""Opt-in Git-only infrastructure identity backup, one repo, no new daemon."""
import hashlib
import io
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import tempfile

REF = "refs/heads/gtm-tenant"
FILES = ("cert.pem", "config.xml", "key.pem")
MAX_BYTES = 4*1024*1024


def command(args, *, data=None, env=None, check=True):
    return subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          check=check, timeout=90, env=env)


def snapshot(home):
    result = {}
    def sig(s):
        return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    signatures = {}
    for name in FILES:
        path = home/name
        fd = os.open(path, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC)
        with os.fdopen(fd,"rb") as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= MAX_BYTES:
                raise ValueError("invalid_identity_file")
            data = stream.read(MAX_BYTES+1)
            if len(data)!=st.st_size or sig(os.fstat(stream.fileno()))!=sig(st):
                raise ValueError("identity_changed")
            result[name] = data
            signatures[name] = sig(st)
    if sum(map(len,result.values()))>MAX_BYTES or any(
            sig((home/n).lstat())!=signatures[n] for n in FILES):
        raise ValueError("identity_changed_or_too_large")
    return result


def backup(repo, home, recipients, remote="origin"):
    if len(recipients)<2 or len(set(recipients))!=len(recipients) or not all(
            re.fullmatch(r"age1[0-9a-z]{58}",r) for r in recipients):
        raise ValueError("independent_age_recipients_required")
    repo,home=Path(repo),Path(home)
    values=snapshot(home)
    h=hashlib.sha256()
    for name,data in values.items():
        h.update(name.encode()+b"\0"+hashlib.sha256(data).digest())
    for recipient in sorted(recipients):
        h.update(recipient.encode()+b"\0")
    fingerprint=h.hexdigest().encode()+b"\n"
    env={k:v for k,v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1",GIT_CONFIG_GLOBAL=os.devnull,
               GIT_NO_REPLACE_OBJECTS="1",GIT_TERMINAL_PROMPT="0",
               GIT_ASKPASS="/usr/local/bin/askpass.sh")
    if Path("/tls/tls.crt").exists():
        env["GIT_SSL_CAINFO"]="/tls/tls.crt"
    base=["git","--git-dir",str(repo),"-c","gc.auto=0",
          "-c","maintenance.auto=false","-c","core.hooksPath=/dev/null",
          "-c","user.name=GettingThingsMarkdown recovery",
          "-c","user.email=recovery@localhost.invalid"]
    def git(*args,data=None,index=None,check=True):
        local=dict(env)
        if index is not None:
            local["GIT_INDEX_FILE"]=index
        return command(base+list(args),data=data,env=local,check=check)
    parent=git("rev-parse","--verify",REF,check=False)
    old=parent.stdout.decode().strip() if parent.returncode==0 else None
    prior=git("show",REF+":identity.sha256",check=False)
    existing=git("cat-file","-t",REF+":identity.tar.gz.age",check=False)
    changed=not (prior.returncode==0 and prior.stdout==fingerprint
                 and existing.returncode==0 and existing.stdout.strip()==b"blob")
    if changed:
        # No plaintext archive on disk. Store only age ciphertext in Git.
        archive=io.BytesIO()
        with tarfile.open(fileobj=archive,mode="w:gz") as tar:
            for name,data in values.items():
                member=tarfile.TarInfo(name)
                member.size=len(data)
                member.mode=0o600
                tar.addfile(member,io.BytesIO(data))
        args=["age"]
        for r in sorted(recipients):
            args+=["-r",r]
        ciphertext=command(args,data=archive.getvalue()).stdout
        if not 0<len(ciphertext)<=MAX_BYTES:
            raise ValueError("encrypted_identity_too_large")
        with tempfile.TemporaryDirectory(prefix="gtmd-identity-index-") as d:
            index=str(Path(d)/"index")
            git("read-tree",old if old else "--empty",index=index)
            for name,body in (("identity.tar.gz.age",ciphertext),("identity.sha256",fingerprint)):
                oid=git("hash-object","-w","--stdin",data=body).stdout.decode().strip()
                git("update-index","--add","--cacheinfo","100644,"+oid+","+name,index=index)
            tree=git("write-tree",index=index).stdout.decode().strip()
            args=["commit-tree",tree,"-m","Encrypted server identity snapshot"]
            if old:
                args+=["-p",old]
            commit=git(*args).stdout.decode().strip()
            # CAS: never lose concurrent tenant configuration updates.
            git("update-ref",REF,commit,old or "0"*len(commit))
    # Always retry push even if identity unchanged. Never force or rewrite.
    git("push",remote,REF+":"+REF)
    return changed


def main():
    try:
        backup("/data/git","/data/syncthing",
               os.environ["GTM_RECOVERY_AGE_RECIPIENT"].split(","))
        print("Encrypted Syncthing identity replicated in tenant Git.")
    except (OSError,ValueError,KeyError,subprocess.SubprocessError):
        print("Encrypted Syncthing identity replication failed.")
        return 1
    return 0


if __name__=="__main__":
    raise SystemExit(main())
