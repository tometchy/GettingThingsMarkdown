#!/usr/bin/env python3
"""Pinned-CA mTLS witness client; no redirects or inherited proxy settings."""
import argparse
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

from recovery_crypto import RecoveryError, canonical, decode, read, MAX


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, endpoint, ca, cert, key):
        url = urllib.parse.urlsplit(endpoint)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ("", "/")):
            raise RecoveryError("invalid_witness_url")
        self.url = endpoint.rstrip("/") + "/v1/recovery"
        tls = ssl.create_default_context(cafile=ca)
        tls.minimum_version = ssl.TLSVersion.TLSv1_3
        tls.load_cert_chain(cert, key)
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=tls), NoRedirect())

    def call(self, value):
        req = urllib.request.Request(self.url, data=canonical(value), method="POST",
                                    headers={"Content-Type": "application/json"})
        try:
            with self.http.open(req, timeout=45) as response:
                return decode(response.read(MAX + 1))
        except urllib.error.HTTPError as exc:
            # Server sends only fixed rejection categories, no credentials.
            code = exc.code
            exc.close()
            raise RecoveryError("witness_http_" + str(code)) from None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for field in ("endpoint", "ca", "cert", "key", "request"):
        p.add_argument("--" + field, required=True)
    args = p.parse_args()
    try:
        print(json.dumps(Client(args.endpoint, args.ca, args.cert, args.key).call(decode(read(args.request)))))
    except (RecoveryError, OSError, ValueError) as exc:
        print(json.dumps({"state": "rejected", "reason": str(exc) if isinstance(exc, RecoveryError) else type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
