#!/usr/bin/env python3
"""Serve only Git upload-pack from a verified, read-only recovery snapshot.

The trusted host must create/isolate the snapshot and provide a signed
witness anchor plus exact refs. No receive-pack, Gitea UI, shell, or writes.
Temporary host service, not a third permanent tenant data copy.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler
import os
from pathlib import Path
import re
import selectors
import subprocess
import time
from urllib.parse import urlsplit

from git_integrity import scan, refs, load_manifest, IntegrityError
from recovery_crypto import verify, decode, read, digest, RecoveryError
from recovery_witness import require
from witness_api import BoundedServer, tls_context

MAX_REQUEST = 8 * 1024 * 1024
CHUNK = 64 * 1024
SERVICE = b"001e# service=git-upload-pack\n0000"


def verify_source(repo, manifest, anchor, public, tenant, generation, readonly=True):
    path = Path(repo).resolve(strict=True)
    require(path.is_dir(), "source_not_directory")
    if readonly:
        require(bool(os.statvfs(path).f_flag & os.ST_RDONLY), "source_mount_not_readonly")
    expected = load_manifest(manifest)
    a = verify(decode(read(anchor)), public)
    require(isinstance(a, dict) and a.get("kind") == "generation"
            and a.get("tenant") == tenant and a.get("generation") == generation
            and a.get("refsDigest") == digest(expected), "source_anchor_mismatch")
    scan(path, expected)
    return path, expected


def make_source(address, tls, repo, expected, fingerprints, timeout=86400):
    require(bool(fingerprints) and all(re.fullmatch(r"[0-9a-f]{64}", x) for x in fingerprints),
            "invalid_source_client_fingerprint")
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def authorized(self):
            cert = self.connection.getpeercert(binary_form=True)
            return bool(cert) and hashlib.sha256(cert).hexdigest() in fingerprints

        def do_GET(self):
            if not self.authorized():
                self.send_error(403)
                return
            if self.path != "/repo.git/info/refs?service=git-upload-pack":
                self.send_error(404)
                return
            self.serve_git(True, b"")

        def do_POST(self):
            if self.path != "/repo.git/git-upload-pack":
                self.send_error(404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_REQUEST or self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/x-git-upload-pack-request":
                    self.send_error(400)
                    return
                body = self.rfile.read(size)
                if len(body) != size:
                    self.send_error(400)
                    return
                if not self.authorized():
                    self.send_error(403)
                    return
                self.serve_git(False, body)
            except (OSError, ValueError):
                self.close_connection = True

        def write_chunk(self, block):
            self.wfile.write(("%x\r\n" % len(block)).encode("ascii") + block + b"\r\n")

        def serve_git(self, advertise, body):
            proc = None
            started = False
            try:
                if refs(repo) != expected:
                    self.send_error(503)
                    return
                # No shell, system/global config, alternate or lazy fetch.
                env = {"PATH": os.environ.get("PATH","/usr/bin:/bin"),
                       "GIT_CONFIG_NOSYSTEM":"1","GIT_CONFIG_GLOBAL":os.devnull,
                       "GIT_NO_REPLACE_OBJECTS":"1","GIT_NO_LAZY_FETCH":"1",
                       "GIT_TERMINAL_PROMPT":"0"}
                args = ["git","-c","uploadpack.allowTipSHA1InWant=false",
                        "-c","uploadpack.allowReachableSHA1InWant=false",
                        "-c","uploadpack.allowAnySHA1InWant=false",
                        "-c","uploadpack.allowFilter=false",
                        "upload-pack","--strict","--stateless-rpc"]
                if advertise:
                    args.append("--advertise-refs")
                args.append(str(repo))
                # Request is small and bounded; spool to a temporary file so
                # there is no stdin/stdout pipe deadlock during a large pack.
                import tempfile
                with tempfile.TemporaryFile() as request:
                    request.write(body)
                    request.seek(0)
                    proc = subprocess.Popen(args,env=env,stdin=request,stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL,start_new_session=True)
                    self.send_response(200)
                    self.send_header("Content-Type","application/x-git-upload-pack-" +
                                     ("advertisement" if advertise else "result"))
                    self.send_header("Cache-Control","no-store")
                    self.send_header("Connection","close")
                    self.send_header("Transfer-Encoding","chunked")
                    self.end_headers()
                    started=True
                    if advertise:
                        self.write_chunk(SERVICE)
                    deadline=time.monotonic()+timeout
                    with selectors.DefaultSelector() as sel:
                        sel.register(proc.stdout,selectors.EVENT_READ)
                        while True:
                            remaining=deadline-time.monotonic()
                            if remaining<=0 or not sel.select(min(remaining,30)):
                                if time.monotonic()>=deadline:
                                    raise TimeoutError("source_transfer_timeout")
                                continue
                            block=os.read(proc.stdout.fileno(),CHUNK)
                            if not block:
                                break
                            self.write_chunk(block)
                    if proc.wait(timeout=5)!=0:
                        raise OSError("upload_pack_failed")
                    # Explicit successful end-of-stream; TLS EOF alone is not
                    # a reliable completion marker for libcurl/GnuTLS.
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
            except (OSError,IntegrityError,subprocess.SubprocessError):
                if not started:
                    self.send_error(503)
            finally:
                self.close_connection=True
                if proc:
                    if proc.poll() is None:
                        import signal
                        try:
                            os.killpg(proc.pid,signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        proc.wait()
                    proc.stdout.close()
    return BoundedServer(address,Handler,tls)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for k in ("repo","manifest","anchor","witness-public","tenant","cert","tls-key","client-ca"):
        p.add_argument("--"+k,required=True)
    p.add_argument("--generation",required=True,type=int)
    p.add_argument("--client-fingerprint",action="append",required=True)
    p.add_argument("--bind",default="127.0.0.1")
    p.add_argument("--port",type=int,default=8444)
    a=p.parse_args()
    repo,expected=verify_source(a.repo,a.manifest,a.anchor,a.witness_public,a.tenant,a.generation)
    with make_source((a.bind,a.port),tls_context(a.cert,a.tls_key,a.client_ca),repo,expected,
                     set(a.client_fingerprint)) as server:
        server.serve_forever()


if __name__=="__main__":
    main()
