#!/usr/bin/env python3
"""Private, ciphertext-only ingress to the existing tenant Git repository."""
import hmac, http.server, json, os, pathlib, re, subprocess, tempfile

ROOT = pathlib.Path(os.environ.get("GTM_MAIL_DATA_ROOT", "/data"))
GIT = ROOT / "git"
WORK = ROOT / "mail"
REF = "refs/heads/gtm-mail"
LIMIT = 100 * 1024 * 1024
TOKEN = os.environ["GTM_SYNCTHING_API_KEY"]

def git(*args, data=None, index=None, check=True):
    env = dict(os.environ, GIT_DIR=str(GIT), GIT_WORK_TREE=str(WORK),
               GIT_ASKPASS="/usr/local/bin/askpass.sh", GIT_TERMINAL_PROMPT="0")
    if pathlib.Path("/tls/tls.crt").exists():
        env["GIT_SSL_CAINFO"] = "/tls/tls.crt"
    if index: env["GIT_INDEX_FILE"] = index
    return subprocess.run(["git", *args], input=data, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          check=check, timeout=90)

def store(digest, body):
    path = ".gtmd/inbox/" + digest + ".json"
    old = git("show", REF + ":" + path, check=False)
    if old.returncode == 0:
        # An SMTP retry after a failed push or lost HTTP response must reuse
        # the already-committed ciphertext and its original message ID.
        body = old.stdout
    else:
        WORK.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            index = str(pathlib.Path(tmp) / "index")
            parent = git("rev-parse", "--verify", REF, check=False)
            if parent.returncode == 0:
                parent_id = parent.stdout.decode().strip()
                git("read-tree", parent_id, index=index)
            else:
                parent_id = None
                git("read-tree", "--empty", index=index)
            oid = git("hash-object", "-w", "--stdin", data=body).stdout.decode().strip()
            git("update-index", "--add", "--cacheinfo", "100644," + oid + "," + path, index=index)
            tree = git("write-tree", index=index).stdout.decode().strip()
            args = ["commit-tree", tree, "-m", "Encrypted inbound mail"]
            if parent_id: args += ["-p", parent_id]
            commit = git(*args).stdout.decode().strip()
            git("update-ref", REF, commit, parent_id or "0" * 40)
    # Keep a ciphertext-only working directory, separate from the native
    # receiveencrypted Syncthing folder. Never put ordinary bytes in that folder.
    target = WORK / path
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as f:
        tmpname = f.name
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmpname, target)
    # A 200 means the same append-only Git ref is present in A and B.
    # A failed push leaves A intact; SMTP retry attempts the same push again.
    git("push", "origin", REF + ":" + REF)
    return body

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_POST(self):
        self.connection.settimeout(120)
        if not hmac.compare_digest(self.headers.get("X-API-Key", ""), TOKEN):
            self.send_error(403)
            return
        match = re.fullmatch(r"/mail/([0-9A-Fa-f]{64})", self.path)
        if not match:
            self.send_error(404); return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if not 0 < n <= LIMIT:
                self.send_error(413); return
            body = self.rfile.read(n)
            if len(body) != n: raise ValueError("short input")
            envelope = json.loads(body)
            # This is a private authenticated endpoint. Only envelope fields,
            # not arbitrary plaintext MIME or paths, are accepted.
            if not all(k in envelope for k in ("id","wrappedKey","nonce","tag","ciphertext","receivedUtc")):
                raise ValueError("not an envelope")
            result = store(match.group(1).lower(), body)
        except (ValueError, OSError, subprocess.SubprocessError):
            self.send_error(503); return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)

if __name__ == "__main__":
    http.server.HTTPServer(("0.0.0.0", 8385), Handler).serve_forever()
