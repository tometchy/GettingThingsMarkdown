#!/usr/bin/env python3
"""Real Git, age, OpenSSL and Syncthing 2.x in the existing Sync image.

Run with --network none. No Syncthing serve, customer data or remote service.
"""
import io
import hashlib
import json
import time
import uuid
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"server/integrity"))
import sync_state as s
import sync_startup as startup
from recovery_crypto import sign, digest
from git_integrity import refs, stage_rebuild, IntegrityError
from recovery_crypto import RecoveryError


def run(*args, data=None):
    return subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          check=True, timeout=30).stdout


class Tests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        self.source = self.root/"source"
        self.source.mkdir()
        self.git("init","-qb","main")
        self.git("config","user.name","CI")
        self.git("config","user.email","ci@example.invalid")
        self.data = b"\x00\xffciphertext\r\n"*20000
        (self.source/"data").mkdir()
        (self.source/"data"/"opaque name").write_bytes(self.data)
        (self.source/"empty").write_bytes(b"")
        self.git("add","--all")
        self.git("commit","-qm","main")
        for branch in ("gtm-vault","gtm-mail"):
            self.git("branch",branch)
        self.home = self.root/"original"
        run("syncthing","generate","--home="+str(self.home),"--no-port-probing")
        self.device = run("syncthing","device-id","--home="+str(self.home)).decode().strip()
        xml = ET.parse(self.home/"config.xml")
        tree = xml.getroot()
        for folder in tree.findall("folder"):
            tree.remove(folder)
        ET.SubElement(tree,"folder",{"id":"tenant-folder","type":"receiveencrypted","path":"/data/files"})
        if not any(d.get("id")==self.device for d in tree.findall("device")):
            ET.SubElement(tree,"device",{"id":self.device})
        xml.write(self.home/"config.xml",encoding="utf-8",xml_declaration=True)
        self.key = self.root/"age.key"
        run("age-keygen","-o",str(self.key))
        self.recipient = run("age-keygen","-y",str(self.key)).decode().strip()
        self.cfg = {"deviceId":self.device,"folderId":"tenant-folder","identityRef":"refs/heads/gtm-tenant"}
        self.git("checkout","-qb","gtm-tenant")
        self.write_bundle()
        self.git("add","identity.tar.gz.age")
        self.git("commit","-qm","encrypted identity")
        self.git("checkout","-q","main")
        self.manifest = refs(self.source/".git")
        self.attempt = self.root/"attempt"
        self.attempt.mkdir()
        self.repo = self.attempt/"repository.git"
        self.dest = self.attempt/"sync-state"
        stage_rebuild(str(self.source/".git"),self.repo,self.manifest,60)

    def git(self,*args):
        return run("git","-C",str(self.source),*args)

    def write_bundle(self, extra=None):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw,mode="w:gz") as t:
            for name in sorted(s.IDENTITY_FILES):
                t.add(self.home/name,arcname=name)
            if extra:
                t.addfile(extra)
        (self.source/"identity.tar.gz.age").write_bytes(run("age","-r",self.recipient,data=raw.getvalue()))

    def prepare(self, cfg=None, key=None):
        return s.prepare(self.repo,self.dest,self.manifest,cfg or self.cfg,key or self.key,60)

    def test_canonical_writer_preserves_tenant_metadata_and_retries(self):
        spec=importlib.util.spec_from_file_location("writer",ROOT/"server/sync/identity-backup-git.py")
        writer=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(writer)
        self.git("checkout","-q","gtm-tenant")
        (self.source/"tenant.json.age").write_bytes(b"opaque tenant configuration")
        self.git("add","tenant.json.age")
        self.git("commit","-qm","tenant metadata")
        self.git("checkout","-q","main")
        remote=self.root/"remote.git"
        run("git","init","--bare",str(remote))
        second=self.root/"second.key"
        run("age-keygen","-o",str(second))
        other=run("age-keygen","-y",str(second)).decode().strip()
        recipients=[self.recipient,other]
        # First push failure must retain the exact committed ciphertext.
        with self.assertRaises(subprocess.CalledProcessError):
            writer.backup(self.source/".git",self.home,recipients,str(self.root/"missing"))
        first=refs(self.source/".git")["refs/heads/gtm-tenant"]
        self.assertFalse(writer.backup(self.source/".git",self.home,recipients,str(remote)))
        self.assertEqual(refs(remote)["refs/heads/gtm-tenant"],first)
        self.assertEqual(self.git("show","gtm-tenant:tenant.json.age"),b"opaque tenant configuration")
        self.assertFalse(writer.backup(self.source/".git",self.home,list(reversed(recipients)),str(remote)))
        self.assertEqual(refs(remote)["refs/heads/gtm-tenant"],first)
        current=refs(self.source/".git")
        for key in (self.key,second):
            result=s.identity_bundle(self.source/".git",current,self.cfg,key,s.time.monotonic()+30)
            self.assertEqual(result["key.pem"],(self.home/"key.pem").read_bytes())
        self.assertEqual((self.source/"data"/"opaque name").read_bytes(),self.data)

    def test_cold_restore_preserves_exact_identity_and_ciphertext_no_new_git(self):
        result = self.prepare()
        self.assertEqual(result["state"],"sync_state_prepared")
        self.assertEqual(result["files"],2)
        self.assertTrue(result["deviceIdVerified"])
        self.assertFalse(result["activationAllowed"])
        self.assertEqual((self.dest/"files"/"data"/"opaque name").read_bytes(),self.data)
        self.assertEqual((self.dest/"files"/"empty").read_bytes(),b"")
        for name in s.IDENTITY_FILES:
            self.assertEqual((self.dest/"syncthing"/name).read_bytes(),(self.home/name).read_bytes())
            self.assertEqual((self.dest/"syncthing"/name).stat().st_mode&0o777,0o600)
        self.assertFalse((self.dest/"files"/".git").exists())
        self.assertEqual(refs(self.repo),self.manifest)
        self.assertEqual(self.prepare(),result)
        (self.dest/"files"/"data"/"opaque name").write_bytes(b"corrupt")
        with self.assertRaises(IntegrityError):
            self.prepare()
        self.assertEqual((self.dest/"files"/"data"/"opaque name").read_bytes(),b"corrupt")

    def test_wrong_key_or_unanchored_ref_never_creates_state(self):
        wrong = self.root/"wrong.key"
        run("age-keygen","-o",str(wrong))
        with self.assertRaisesRegex(IntegrityError,"identity_decryption_failed"):
            self.prepare(key=wrong)
        with self.assertRaisesRegex(RecoveryError,"identity_ref_not_anchored"):
            self.prepare(cfg={**self.cfg,"identityRef":"refs/heads/gtm-recovery"})
        self.assertFalse(self.dest.exists())

    def test_insufficient_capacity_blocks_before_state_creation(self):
        fs=os.statvfs(self.attempt)
        with patch.object(s.os,"statvfs",return_value=fs._replace(f_bavail=0) if hasattr(fs,"_replace")
                          else os.statvfs_result((fs.f_bsize,fs.f_frsize,fs.f_blocks,fs.f_bfree,
                              0,fs.f_files,fs.f_ffree,fs.f_favail,fs.f_flag,fs.f_namemax))):
            with self.assertRaisesRegex(RecoveryError,"insufficient_sync_staging_capacity"):
                self.prepare()
        self.assertFalse(self.dest.exists())

    def test_strict_startup_real_git_syncthing_and_signed_permit(self):
        self.prepare()
        data=self.root/"live"
        trust=self.root/"trust"
        data.mkdir()
        trust.mkdir()
        self.repo.rename(data/"git")
        (self.dest/"files").rename(data/"files")
        (self.dest/"syncthing").rename(data/"syncthing")
        tenant="gtm-"+"b"*32
        ids={"podUid":str(uuid.uuid4()),"nodeUid":str(uuid.uuid4()),
             "pvcUids":sorted([str(uuid.uuid4()),str(uuid.uuid4())])}
        cfg={"schema":1,"tenant":tenant,"role":"sync","identity":ids,"epoch":1,
             "mounts":{"data":"253:1","files":"253:2"},"refs":self.manifest,
             "deviceId":self.device,"folderId":self.cfg["folderId"],
             "identitySha256":{n:hashlib.sha256((data/"syncthing"/n).read_bytes()).hexdigest()
                               for n in s.IDENTITY_FILES}}
        key=trust/"witness.key"
        pub=trust/"witness.pub"
        run("openssl","genpkey","-algorithm","ED25519","-out",str(key))
        run("openssl","pkey","-in",str(key),"-pubout","-out",str(pub))
        now=int(time.time())
        permit={"schema":1,"kind":"writer_permit","tenant":tenant,"role":"sync",
                "epoch":1,"identity":ids,"sequence":1,"issuedUnix":now,"expiresUnix":now+60}
        (trust/"permit.json").write_text(json.dumps(sign(permit,key)))
        cfg["anchor"]=sign({"schema":1,"kind":"generation","tenant":tenant,"generation":1,
                           "previousDigest":None,"refsDigest":digest(self.manifest),
                           "committedUnix":now},key)
        def save():
            (trust/"startup.json").write_text(json.dumps(cfg))
        save()
        fake_mountinfo=trust/"mountinfo"
        fake_mountinfo.write_text("")
        # The real LVM job checks kernel mount identity independently.
        # This image test exercises actual signed permit, Git and Syncthing.
        with patch.object(startup,"check_mount"),patch.object(startup,"trusted_file",side_effect=Path):
            def preflight():
                return startup.preflight(data,trust,tenant,ids,timeout=60,mountinfo=fake_mountinfo)
            self.assertEqual(preflight()["files"],2)
            self.assertFalse(preflight()["activationAuthorizedByPreflight"])
            old_anchor=cfg["anchor"]
            cfg["anchor"]=sign({**old_anchor["payload"],"refsDigest":"0"*64},key)
            save()
            with self.assertRaisesRegex(RecoveryError,"startup_anchor_mismatch"):
                preflight()
            cfg["anchor"]=old_anchor
            cfg["epoch"]=2
            save()
            with self.assertRaisesRegex(RecoveryError,"startup_epoch_mismatch"):
                preflight()
            cfg["epoch"]=1
            cfg["mounts"]["files"]=cfg["mounts"]["data"]
            save()
            with self.assertRaisesRegex(RecoveryError,"invalid_startup_mounts"):
                preflight()
            cfg["mounts"]["files"]="253:2"
            save()
            original=(data/"syncthing"/"config.xml").read_bytes()
            (data/"syncthing"/"config.xml").write_bytes(b"wrong")
            with self.assertRaisesRegex(RecoveryError,"startup_identity_digest_mismatch"):
                preflight()
            (data/"syncthing"/"config.xml").write_bytes(original)
            run("git","--git-dir",str(data/"git"),"symbolic-ref","HEAD","refs/heads/gtm-tenant")
            with self.assertRaisesRegex(RecoveryError,"startup_head_not_main"):
                preflight()
            run("git","--git-dir",str(data/"git"),"symbolic-ref","HEAD","refs/heads/main")
            (data/"files"/"empty").unlink()
            with self.assertRaisesRegex(IntegrityError,"missing_worktree_file"):
                preflight()
            self.assertFalse((data/"files"/"empty").exists())
            self.assertEqual(refs(data/"git"),self.manifest)

    def test_wrong_device_id_rejected(self):
        cfg={**self.cfg,"deviceId":"AAAAAAA-"*7+"AAAAAAA"}
        with self.assertRaises(RecoveryError):
            self.prepare(cfg=cfg)
        self.assertFalse((self.dest/"ready.json").exists())

    def test_mismatched_key_and_cert_rejected(self):
        other=self.root/"other"
        run("syncthing","generate","--home="+str(other),"--no-port-probing")
        bundle={n:(self.home/n).read_bytes() for n in s.IDENTITY_FILES}
        bundle["key.pem"]=(other/"key.pem").read_bytes()
        home=self.root/"mismatch"
        home.mkdir()
        for name,data in bundle.items():
            (home/name).write_bytes(data)
            (home/name).chmod(0o600)
        # Syncthing may reject a mismatched pair itself; either check blocks.
        with self.assertRaises((RecoveryError,IntegrityError)):
            s.verify_identity(home,bundle,self.cfg,s.time.monotonic()+30)

    def test_unsafe_archive(self):
        self.git("checkout","-q","gtm-tenant")
        extra=tarfile.TarInfo("evil")
        extra.type=tarfile.SYMTYPE
        extra.linkname="/etc/passwd"
        self.write_bundle(extra)
        self.git("add","identity.tar.gz.age")
        self.git("commit","-qm","malformed")
        manifest=refs(self.source/".git")
        with self.assertRaisesRegex(RecoveryError,"invalid_identity_archive_member"):
            s.identity_bundle(self.source/".git",manifest,self.cfg,self.key,s.time.monotonic()+30)
        self.assertFalse(self.dest.exists())


if __name__=="__main__":
    unittest.main()
