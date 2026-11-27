"""Linearizable etcd v3 gateway storage for signed recovery metadata only.

Compare mod_revision, never blindly overwrite. No transaction retry on an
ambiguous transport failure; caller must read current state again.
"""
import base64
import ssl
import urllib.error
import urllib.parse
import urllib.request

from recovery_crypto import RecoveryError, canonical, decode, read

PREFIX = b"/gtmd/recovery/v1/"
MAX_VALUE = 512 * 1024
MAX_RESPONSE = 2 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def b64(raw):
    return base64.b64encode(raw).decode("ascii")


class EtcdStore:
    def __init__(self, endpoints, ca=None, cert=None, key=None, *, insecure_test=False, timeout=8):
        if not isinstance(endpoints, list) or not endpoints:
            raise RecoveryError("etcd_endpoints_missing")
        self.endpoints = []
        self.timeout = timeout
        for endpoint in endpoints:
            url = urllib.parse.urlsplit(endpoint)
            if (url.scheme != ("http" if insecure_test else "https") or not url.hostname
                    or url.username or url.password or url.query or url.fragment or url.path not in ("", "/")):
                raise RecoveryError("invalid_etcd_endpoint")
            if insecure_test and url.hostname not in ("127.0.0.1", "localhost"):
                raise RecoveryError("insecure_etcd_only_loopback_test")
            self.endpoints.append(endpoint.rstrip("/"))
        if insecure_test:
            self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        else:
            if not all((ca, cert, key)):
                raise RecoveryError("etcd_mtls_required")
            tls = ssl.create_default_context(cafile=ca)
            tls.minimum_version = ssl.TLSVersion.TLSv1_2
            tls.load_cert_chain(cert, key)
            self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=tls), NoRedirect())

    @classmethod
    def from_config(cls, path):
        cfg = decode(read(path))
        if not isinstance(cfg, dict) or set(cfg) != {"endpoints", "ca", "cert", "key"}:
            raise RecoveryError("invalid_etcd_config")
        return cls(**cfg)

    def request(self, endpoint, method, value):
        req = urllib.request.Request(endpoint + method, data=canonical(value),
            method="POST", headers={"Content-Type":"application/json"})
        try:
            with self.http.open(req, timeout=self.timeout) as res:
                raw = res.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise RecoveryError("etcd_response_too_large")
                result = decode(raw)
                if not isinstance(result, dict) or "error" in result:
                    raise RecoveryError("etcd_rejected")
                return result
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise RecoveryError("etcd_unavailable") from exc

    def key(self, tenant):
        import re
        if not isinstance(tenant, str) or not re.fullmatch(r"gtm-[0-9a-f]{32}",tenant):
            raise RecoveryError("invalid_etcd_key")
        return b64(PREFIX + tenant.encode("ascii"))

    def read(self, tenant):
        key = self.key(tenant)
        for endpoint in self.endpoints:
            try:
                result = self.request(endpoint, "/v3/kv/range", {"key":key,"serializable":False})
                values = result.get("kvs",[])
                if not values:
                    return None, 0
                if len(values)!=1 or values[0]["key"]!=key:
                    raise RecoveryError("invalid_etcd_range")
                raw = base64.b64decode(values[0]["value"],validate=True)
                revision = int(values[0]["mod_revision"])
                if not 0<len(raw)<=MAX_VALUE or revision<=0:
                    raise RecoveryError("invalid_etcd_value")
                return raw, revision
            except (RecoveryError, ValueError, KeyError, TypeError):
                continue
        raise RecoveryError("etcd_linearizable_read_unavailable")

    def cas(self, tenant, revision, raw):
        if type(revision) is not int or revision < 0 or not 0<len(raw)<=MAX_VALUE:
            raise RecoveryError("invalid_etcd_transaction")
        key=self.key(tenant)
        comparison={"key":key,"result":"EQUAL","target":"MOD" if revision else "VERSION",
                    ("mod_revision" if revision else "version"):str(revision)}
        value={"compare":[comparison],"success":[{"request_put":{"key":key,"value":b64(raw)}}],"failure":[]}
        for endpoint in self.endpoints:
            try:
                self.request(endpoint,"/v3/kv/range",{"key":key,"serializable":False})
            except RecoveryError:
                continue
            try:
                result=self.request(endpoint,"/v3/kv/txn",value)
            except RecoveryError as exc:
                raise RecoveryError("etcd_transaction_outcome_unknown") from exc
            return result.get("succeeded",False) is True
        raise RecoveryError("etcd_quorum_unavailable")
