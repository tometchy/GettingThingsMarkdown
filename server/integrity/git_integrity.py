#!/usr/bin/env python3
"""Read-only Git integrity worker and isolated, manifest-pinned rebuild.

A successful scan is NOT proof of a physically uncached disk read, current
cross-cloud agreement, or immutable retention. No live cutover is performed.
"""
import argparse
import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import math
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
DAY = 86400
CRITICAL = 2 * DAY


class IntegrityError(Exception):
    pass


def git(repo, *args, timeout=DAY, capture=False):
    # Do not inherit repository/index/alternate overrides from callers.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") or k in (
               "GIT_ASKPASS", "GIT_SSL_CAINFO", "GIT_SSL_CAPATH",
               "GIT_TERMINAL_PROMPT", "GIT_SSL_CERT", "GIT_SSL_KEY")}
    env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1",
               GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0")
    command = ["git", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
               "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
               "-c", "protocol.https.allow=always"]
    if repo is not None:
        command += ["--git-dir", str(Path(repo).resolve())]
    try:
        result = subprocess.run(command + list(args), env=env,
            stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            timeout=timeout, check=True)
    except (subprocess.SubprocessError, OSError) as exc:
        # Do not expose Git diagnostics (paths, refs, credentials) to logs.
        raise IntegrityError("git_" + args[0] + "_failed") from exc
    return result.stdout.decode("utf-8", errors="strict") if capture else None


def refs(repo, timeout=60):
    output = git(repo, "for-each-ref", "--format=%(refname) %(objectname)",
                 capture=True, timeout=timeout)
    result = {}
    for line in output.splitlines():
        name, sep, oid = line.partition(" ")
        if not sep or not OID.fullmatch(oid) or name in result or name.startswith("refs/replace/"):
            raise IntegrityError("invalid_ref")
        result[name] = oid
    return result


def complete_repo(repo):
    repo = Path(repo)
    if not (repo / "HEAD").is_file():
        raise IntegrityError("repository_missing")
    for name in ("objects/info/alternates", "objects/info/http-alternates",
                 "shallow", "info/grafts"):
        if (repo / name).exists():
            raise IntegrityError("external_or_incomplete_object_store")
    if any((repo / "objects/pack").glob("*.promisor")):
        raise IntegrityError("partial_clone")


def scan(repo, expected=None, timeout=DAY):
    """Verify all stored objects and reflogs, not just main or reachable blobs."""
    repo = Path(repo)
    complete_repo(repo)
    started = time.monotonic()
    def remaining():
        budget = timeout - (time.monotonic() - started)
        if budget <= 0:
            raise IntegrityError("scan_timeout")
        return budget
    before = refs(repo, min(60, remaining()))
    if "refs/heads/main" not in before:
        raise IntegrityError("main_missing")
    if expected is not None and before != expected:
        raise IntegrityError("manifest_mismatch")
    # fsck reads loose and packed objects; don't skip unreachable objects.
    git(repo, "fsck", "--full", "--strict", "--no-dangling", "--no-progress",
        timeout=remaining())
    # Validate every existing index; detect missing/orphan pack pairs too.
    packs = set((repo / "objects/pack").glob("*.pack"))
    indexes = set((repo / "objects/pack").glob("*.idx"))
    if {p.with_suffix(".idx") for p in packs} != indexes:
        raise IntegrityError("pack_index_mismatch")
    for idx in sorted(indexes):
        git(repo, "verify-pack", str(idx.resolve()), timeout=remaining())
    after = refs(repo, min(60, remaining()))
    if before != after:
        raise IntegrityError("refs_changed_during_scan")
    if packs != set((repo / "objects/pack").glob("*.pack")) or indexes != set((repo / "objects/pack").glob("*.idx")):
        raise IntegrityError("packs_changed_during_scan")
    return {"refs": len(after), "packs": len(packs),
            "durationSeconds": round(time.monotonic() - started, 3)}


def load_manifest(path):
    # The caller must authenticate the manifest via the independent coordinator.
    # This primitive validates exact refs, not signatures or fencing authority.
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict) or "refs/heads/main" not in raw:
        raise IntegrityError("manifest_missing_main")
    for name, oid in raw.items():
        if (not isinstance(name, str) or not name.startswith("refs/") or
                name.startswith("refs/replace/") or not isinstance(oid, str) or
                not OID.fullmatch(oid)):
            raise IntegrityError("invalid_manifest")
        git(None, "check-ref-format", name, timeout=10)
    if len({len(oid) for oid in raw.values()}) != 1:
        raise IntegrityError("mixed_object_format")
    return raw


def stage_rebuild(source, destination, expected, timeout=DAY):
    """Fetch exact trusted refs into a NEW bare repo; never alter live repos.

    Unreachable objects/reflog-only history cannot be recovered by this
    protocol. All retained objects must be rooted by protected refs before
    a future coordinator may authorize cutover.
    """
    dest = Path(destination).absolute()
    # mkdir with exist_ok=False refuses existing data, including symlinks.
    dest.mkdir(mode=0o700)
    fmt = "sha256" if len(next(iter(expected.values()))) == 64 else "sha1"
    git(None, "init", "--bare", "--object-format=" + fmt, str(dest), timeout=60)
    # Fetch via upload-pack, even for a local source; no hardlinks/alternates.
    # Exact wildcard preserves branches, tags and protected refs.
    git(dest, "-c", "fetch.fsckObjects=true", "fetch", "--no-tags", "--",
        source, "+refs/*:refs/*", timeout=timeout)
    git(dest, "symbolic-ref", "HEAD", "refs/heads/main", timeout=60)
    return scan(dest, expected, timeout=timeout)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as tmp:
        name = tmp.name
        try:
            json.dump(value, tmp, separators=(",", ":"))
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
    try:
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def emit(role, state, **data):
    event = {"event": "gtmd_git_integrity", "role": role, "state": state,
             "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
             "updatedUnix": time.time(), **data}
    print(json.dumps(event, separators=(",", ":")), flush=True)
    return event


def cycle(repo, status, role, timeout):
    previous = None
    try:
        previous = json.loads(Path(status).read_text()).get("lastSuccessfulUnix")
        if not isinstance(previous, (int, float)) or not 0 < previous <= time.time():
            previous = None
    except (OSError, ValueError, AttributeError):
        pass
    started = time.time()
    atomic_json(status, emit(role, "scanning", startedUnix=started,
                            lastSuccessfulUnix=previous))
    try:
        result = scan(repo, timeout=timeout)
    except (IntegrityError, OSError, UnicodeError) as exc:
        reason = str(exc) if isinstance(exc, IntegrityError) else type(exc).__name__
        state = "unverified" if reason in ("refs_changed_during_scan",
                 "packs_changed_during_scan", "main_missing", "repository_missing") else "degraded"
        age = None if previous is None else max(0, time.time() - previous)
        severity = "critical" if state == "degraded" or age is None or age >= CRITICAL else (
                   "warning" if age >= DAY else "info")
        event = emit(role, state, reason=reason, severity=severity,
                     lastSuccessfulUnix=previous, ageSeconds=age)
    else:
        # Conservative: oldest bytes may have been read at scan start.
        age = time.time() - started
        severity = "critical" if age >= CRITICAL else "warning" if age >= DAY else "info"
        event = emit(role, "verified", severity=severity,
                     lastSuccessfulUnix=started, ageSeconds=age, **result)
    atomic_json(status, event)
    return event


def status_health(path, now=None, scan_timeout=DAY, interval=300):
    """Independent local watchdog. Does not depend on scanner progress."""
    now = time.time() if now is None else now
    try:
        value = json.loads(Path(path).read_text())
        updated = value["updatedUnix"]
        state = value["state"]
        if (not isinstance(updated, (int, float)) or not 0 < updated <= now
                or state not in ("scanning", "verified", "unverified", "degraded")):
            return "critical", "invalid_status"
        allowed = scan_timeout + 120 if state == "scanning" else interval + 120
        if now - updated > allowed:
            return "critical", "scanner_stalled"
        if state == "degraded":
            return "critical", "corruption_or_scan_failure"
        last = value.get("lastSuccessfulUnix")
        if last is None:
            # An initial scan has no previous verification; still unverified.
            return ("warning", "never_verified") if now - updated < DAY else ("critical", "never_verified")
        if not isinstance(last, (int, float)) or not 0 < last <= now:
            return "critical", "invalid_status"
        age = now - last
        if age >= CRITICAL:
            return "critical", "verification_overdue"
        if age >= DAY:
            return "warning", "verification_overdue"
        return "info", "within_budget"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "critical", "status_missing_or_invalid"


def metrics_text(path):
    """Minimal private metrics, no refs, paths, tenant content or error details."""
    severity, _ = status_health(path)
    last = 0
    try:
        value = json.loads(Path(path).read_text()).get("lastSuccessfulUnix")
        if type(value) in (int, float) and math.isfinite(value) and 0 < value <= time.time():
            last = value
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    lines = ["# TYPE gtmd_git_integrity_health gauge"]
    for level in ("info", "warning", "critical"):
        lines.append(f'gtmd_git_integrity_health{{severity="{level}"}} {int(level == severity)}')
    lines.extend(["# TYPE gtmd_git_integrity_last_success_unixtime gauge",
                  f"gtmd_git_integrity_last_success_unixtime {last:.3f}"])
    return "\n".join(lines) + "\n"


def serve_metrics(path, port):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(3)
            super().setup()

        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            payload = metrics_text(path).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    # Only reachable from private monitoring peers via tenant NetworkPolicy.
    # No credentials or mutation endpoints; bounded single-connection handler.
    with HTTPServer(("0.0.0.0", port), Handler) as http:
        http.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("scan")
    p.add_argument("repo")
    p.add_argument("--manifest")
    p.add_argument("--timeout", type=int, default=DAY)
    p = sub.add_parser("watch")
    p.add_argument("repo")
    p.add_argument("status")
    p.add_argument("--role", choices=("sync", "history"), required=True)
    p.add_argument("--interval", type=int, default=300)
    p.add_argument("--timeout", type=int, default=DAY)
    p = sub.add_parser("watchdog")
    p.add_argument("status")
    p.add_argument("--role", choices=("sync", "history"), required=True)
    p = sub.add_parser("metrics")
    p.add_argument("status")
    p.add_argument("--port", type=int, default=8390)
    p = sub.add_parser("stage-rebuild")
    p.add_argument("source")
    p.add_argument("destination")
    p.add_argument("manifest")
    p.add_argument("--timeout", type=int, default=DAY)
    args = parser.parse_args()
    if getattr(args, "timeout", DAY) <= 0 or getattr(args, "interval", 1) <= 0:
        parser.error("timeouts and intervals must be positive")
    try:
        if args.action == "watch":
            # Lower CPU priority, no root required.
            os.nice(10)
            while True:
                cycle(args.repo, args.status, args.role, args.timeout)
                time.sleep(args.interval)
        elif args.action == "metrics":
            serve_metrics(args.status, args.port)
        elif args.action == "watchdog":
            # Separate process from the scanner; reports stalled/missing state.
            # Cluster-level monitoring must still detect a missing entire pod.
            time.sleep(120)
            while True:
                severity, reason = status_health(args.status)
                emit(args.role, "watchdog", severity=severity, reason=reason)
                time.sleep(60)
        elif args.action == "scan":
            expected = load_manifest(args.manifest) if args.manifest else None
            print(json.dumps(scan(args.repo, expected, args.timeout)))
        else:
            print(json.dumps(stage_rebuild(args.source, args.destination,
                                           load_manifest(args.manifest), args.timeout)))
    except (IntegrityError, OSError, ValueError) as exc:
        print(json.dumps({"event": "gtmd_git_integrity", "state": "failed",
                          "reason": str(exc) if isinstance(exc, IntegrityError) else type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
