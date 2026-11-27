import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"server/integrity"))
import recovery_source as source
import recovery_crypto as crypto
import git_integrity as git

TENANT="gtm-"+"d"*32


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.repo=self.root/"work"
        self.repo.mkdir()
        self.git("init","-qb","main")
        self.git("config","user.name","CI")
        self.git("config","user.email","ci@example.invalid")
        (self.repo/"file").write_bytes(b"opaque encrypted bytes")
        self.git("add","file")
        self.git("commit","-qm","first")
        for role in ("gtm-tenant","gtm-vault","gtm-mail"):
            self.git("branch",role)
        self.git("update-ref","refs/gtmd/protected/hold","HEAD")
        self.openssl("genpkey","-algorithm","ED25519","-out","witness.key")
        self.openssl("pkey","-in","witness.key","-pubout","-out","witness.pub")
        self.expected=git.refs(self.repo/".git")
        self.manifest=self.root/"refs.json"
        self.manifest.write_text(json.dumps(self.expected))
        self.anchor=self.root/"anchor.json"
        self.anchor.write_text(json.dumps(crypto.sign({"schema":1,"kind":"generation",
                "tenant":TENANT,"generation":1,"refsDigest":crypto.digest(self.expected)},
                self.root/"witness.key")))
        self.openssl("req","-x509","-newkey","rsa:2048","-nodes","-days","1",
             "-subj","/CN=CI CA","-keyout","ca.key","-out","ca.crt",
             "-addext","basicConstraints=critical,CA:TRUE",
             "-addext","keyUsage=critical,keyCertSign,cRLSign",
             "-addext","subjectKeyIdentifier=hash")
        for n in ("server","target","other"):
            self.openssl("req","-new","-newkey","rsa:2048","-nodes","-subj","/CN="+n,
                         "-keyout",n+".key","-out",n+".csr")
            (self.root/(n+".ext")).write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n"+("subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n" if n=="server" else "extendedKeyUsage=clientAuth\n"))
            self.openssl("x509","-req","-in",n+".csr","-CA","ca.crt","-CAkey","ca.key",
                         "-CAcreateserial","-days","1","-extfile",n+".ext","-out",n+".crt")
        fp=hashlib.sha256(ssl.PEM_cert_to_DER_cert((self.root/"target.crt").read_text())).hexdigest()
        repo,expected=source.verify_source(self.repo/".git",self.manifest,self.anchor,
                    self.root/"witness.pub",TENANT,1,readonly=False)
        tls=source.tls_context(self.root/"server.crt",self.root/"server.key",self.root/"ca.crt")
        self.server=source.make_source(("127.0.0.1",0),tls,repo,expected,{fp},60)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url="https://127.0.0.1:"+str(self.server.server_address[1])+"/repo.git"

    def git(self,*args):
        return subprocess.check_output(["git","-C",str(self.repo),*args],stderr=subprocess.DEVNULL)

    def openssl(self,*args):
        subprocess.run(["openssl",*args],cwd=self.root,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

    def test_real_git_https_readonly_restore(self):
        env={k:str(self.root/v) for k,v in (("GIT_SSL_CAINFO","ca.crt"),("GIT_SSL_CERT","target.crt"),
                                           ("GIT_SSL_KEY","target.key"))}
        with patch.dict(os.environ,env):
            staged=self.root/"staged.git"
            git.stage_rebuild(self.url,staged,self.expected,60)
            self.assertEqual(git.refs(staged),self.expected)
            with self.assertRaises(git.IntegrityError):
                git.git(staged,"push",self.url,"main:refs/heads/forbidden",timeout=15)
        self.assertEqual(git.refs(self.repo/".git"),self.expected)

    def test_wrong_certificate_and_unverified_mount(self):
        ctx=ssl.create_default_context(cafile=str(self.root/"ca.crt"))
        ctx.load_cert_chain(self.root/"other.crt",self.root/"other.key")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.url+"/info/refs?service=git-upload-pack",context=ctx,timeout=10)
        self.assertEqual(cm.exception.code,403)
        cm.exception.close()
        with self.assertRaises(crypto.RecoveryError):
            source.verify_source(self.repo/".git",self.manifest,self.anchor,self.root/"witness.pub",TENANT,2,readonly=False)


if __name__=="__main__":
    unittest.main()
