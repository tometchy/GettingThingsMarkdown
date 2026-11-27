"""Small bounded Kubernetes client, no external dependencies or redirects."""
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_RESPONSE = 32 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, code, reservation_policy_denied=False, category="api_rejected"):
        self.code = code
        self.reservation_policy_denied = reservation_policy_denied
        self.category = category
        super().__init__("kubernetes_http_" + str(code))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Kube:
    def __init__(self, endpoint, ca, token):
        url = urllib.parse.urlsplit(endpoint)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            raise ValueError("invalid_api_endpoint")
        self.endpoint = endpoint.rstrip("/")
        self.token = Path(token)
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca)),
            NoRedirect())

    def request(self, method, path, value=None, content_type="application/json"):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("invalid_api_path")
        headers = {"Authorization": "Bearer " + self.token.read_text().strip(),
                   "Accept": "application/json"}
        payload = None
        if value is not None:
            payload = json.dumps(value, separators=(",", ":")).encode()
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self.endpoint + path, data=payload,
                                     headers=headers, method=method)
        try:
            with self.http.open(req, timeout=20) as res:
                raw = res.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise ValueError("api_response_too_large")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            # Recognize only the expected admission denial. Never log the body.
            denied = False
            category = "api_rejected"
            try:
                body = json.loads(exc.read(65536))
                message = body.get("message", "")
                denied = (exc.code == 403 and isinstance(message, str)
                          and "gtmd-storage-reservation-only" in message
                          and "Only the quarantine claim reservation may be added" in message)
                if denied:
                    category = "expected_admission_denial"
                elif isinstance(message, str) and "gtmd-storage-reservation-only" in message:
                    category = "reservation_admission_rejected"
                elif exc.code == 403:
                    category = "forbidden"
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            raise ApiError(exc.code, denied, category) from None

    def get(self, path):
        return self.request("GET", path)

    def list(self, path):
        token = ""
        for _ in range(10000):
            query = {"limit": 250}
            if token:
                query["continue"] = token
            sep = "&" if "?" in path else "?"
            result = self.get(path + sep + urllib.parse.urlencode(query))
            yield from result["items"]
            token = result["metadata"].get("continue", "")
            if not token:
                return
        raise ValueError("api_list_limit")

    def patch(self, path, operations):
        return self.request("PATCH", path, operations, "application/json-patch+json")
