import hashlib
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"server/integrity"))
import witness_api as api
import witness_client as client
import recovery_crypto as crypto

TENANT="gtm-"+"a"*32


class Fake:
    def status(self, tenant):
        return {"generation":7,"revision":9,"anchorDigest":"x","repair":None,"challenge":None}

    def challenge(self,*args):
        return {"challenge":list(args)}


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.run_openssl("req","-x509","-newkey","rsa:2048","-nodes","-days","1",
                         "-subj","/CN=CI CA","-keyout","ca.key","-out","ca.crt",
                         "-addext","basicConstraints=critical,CA:TRUE",
                         "-addext","keyUsage=critical,keyCertSign,cRLSign",
                         "-addext","subjectKeyIdentifier=hash")
        for name in ("server","coordinator","other"):
            self.run_openssl("req","-new","-newkey","rsa:2048","-nodes","-subj","/CN="+name,
                             "-keyout",name+".key","-out",name+".csr")
            ext=self.root/(name+".ext")
            ext.write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n" + ("subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n" if name=="server" else "extendedKeyUsage=clientAuth\n"))
            self.run_openssl("x509","-req","-in",name+".csr","-CA","ca.crt","-CAkey","ca.key",
                             "-CAcreateserial","-days","1","-extfile",name+".ext","-out",name+".crt")
        der=ssl.PEM_cert_to_DER_cert((self.root/"coordinator.crt").read_text())
        fingerprint=hashlib.sha256(der).hexdigest()
        tls=api.tls_context(self.root/"server.crt",self.root/"server.key",self.root/"ca.crt")
        self.server=api.make_server(("127.0.0.1",0),tls,Fake(),{fingerprint})
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url="https://127.0.0.1:"+str(self.server.server_address[1])

    def run_openssl(self,*args):
        subprocess.run(["openssl",*args],cwd=self.root,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

    def connect(self,name):
        return client.Client(self.url,str(self.root/"ca.crt"),str(self.root/(name+".crt")),str(self.root/(name+".key")))

    def test_authenticated_coordinator(self):
        c=self.connect("coordinator")
        self.assertEqual(c.call({"tenant":TENANT,"action":"status"})["generation"],7)
        result=c.call({"tenant":TENANT,"action":"challenge","kind":"anchor","targetRole":None,"targetPvcUid":None})
        self.assertEqual(result["challenge"][0],TENANT)

    def test_other_valid_certificate_denied(self):
        with self.assertRaisesRegex(crypto.RecoveryError,"witness_http_403"):
            self.connect("other").call({"tenant":TENANT,"action":"status"})

    def test_no_remote_genesis_or_activation(self):
        c=self.connect("coordinator")
        for action in ("initialize","activate","fence"):
            with self.assertRaisesRegex(crypto.RecoveryError,"witness_http_409"):
                c.call({"tenant":TENANT,"action":action})


if __name__=="__main__":
    unittest.main()
