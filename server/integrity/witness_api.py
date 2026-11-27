#!/usr/bin/env python3
"""Private mTLS API for the independent metadata witness.

Only explicitly pinned coordinator certificates may invoke it. The
coordinator still cannot forge replica attestations or enable live cutover.
Initialization remains an offline operator action, not an API endpoint.
"""
import argparse
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import ssl
import threading

from recovery_crypto import RecoveryError, canonical, decode, MAX
from recovery_witness import Witness, config, require, TENANT


def dispatch(witness, data):
    require(isinstance(data, dict) and isinstance(data.get("tenant"), str)
            and TENANT.fullmatch(data["tenant"]), "invalid_request")
    tenant = data["tenant"]
    operation = data.get("action")
    base = {"tenant", "action"}
    fields = {"status": set(), "challenge": {"kind", "targetRole", "targetPvcUid"},
              "anchor": {"sync", "history"}, "start-repair": {"evidence"},
              "ticket": {"operation"}, "receipt": {"operation", "evidence"},
              "abort": {"operation"}}
    require(isinstance(operation, str) and operation in fields and set(data) == base | fields[operation],
            "invalid_request_fields")
    if operation == "status":
        s = witness.status(tenant)
        return {"tenant": tenant, "generation": s["generation"], "revision": s["revision"],
                "anchorDigest": s["anchorDigest"], "repair": s["repair"],
                "challenge": s["challenge"], "activationAllowed": False}
    if operation == "challenge":
        return witness.challenge(tenant, data["kind"], data["targetRole"], data["targetPvcUid"])
    if operation == "anchor":
        return witness.anchor(tenant, data["sync"], data["history"])
    if operation == "start-repair":
        return witness.start_repair(tenant, data["evidence"])
    if operation == "ticket":
        raw, sig = witness.ticket(tenant, data["operation"])
        return {"ticket": base64.b64encode(raw).decode(), "signature": base64.b64encode(sig).decode(),
                "activationAllowed": False}
    if operation == "receipt":
        return witness.receipt(tenant, data["operation"], data["evidence"])
    return witness.abort(tenant, data["operation"])


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, addr, handler, tls):
        super().__init__(addr, handler)
        self.tls = tls
        self.slots = threading.BoundedSemaphore(8)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            request.settimeout(5)
            with self.tls.wrap_socket(request, server_side=True) as secured:
                self.finish_request(secured, address)
        except (OSError, ssl.SSLError):
            pass
        finally:
            self.shutdown_request(request)
            self.slots.release()


def make_server(address, tls, witness, fingerprints):
    require(bool(fingerprints) and all(re.fullmatch(r"[0-9a-f]{64}", fp) for fp in fingerprints),
            "invalid_coordinator_certificate_fingerprint")
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, code, value):
            payload = canonical(value)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            cert = self.connection.getpeercert(binary_form=True)
            if not cert or hashlib.sha256(cert).hexdigest() not in fingerprints:
                self.send_json(403, {"error": "coordinator_not_authorized"})
                return
            if self.path != "/v1/recovery":
                self.send_json(404, {"error": "not_found"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                require(0 < size <= MAX and self.headers.get_content_type() == "application/json"
                        and not self.headers.get("Transfer-Encoding"), "invalid_request_size")
                raw = self.rfile.read(size)
                require(len(raw) == size, "truncated_request")
                self.send_json(200, dispatch(witness, decode(raw)))
            except (RecoveryError, ValueError, KeyError, TypeError, AttributeError) as exc:
                self.send_json(409, {"error": str(exc) if isinstance(exc, RecoveryError) else "invalid_request"})
            except OSError:
                self.send_json(503, {"error": "witness_unavailable"})
    return BoundedServer(address, Handler, tls)


def tls_context(cert, key, client_ca):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(cert, key)
    ctx.load_verify_locations(cafile=client_ca)
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for field in ("state-dir", "config", "key", "cert", "tls-key", "client-ca"):
        p.add_argument("--" + field, required=True)
    p.add_argument("--coordinator-fingerprint", action="append", required=True)
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8443)
    args = p.parse_args()
    w = Witness(args.state_dir, config(args.config, args.key), args.key)
    tls = tls_context(args.cert, args.tls_key, args.client_ca)
    with make_server((args.bind, args.port), tls, w, set(args.coordinator_fingerprint)) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
