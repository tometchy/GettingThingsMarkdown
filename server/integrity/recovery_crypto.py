"""Bounded canonical JSON and Ed25519 signatures for recovery metadata."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

MAX = 4 * 1024 * 1024
PREFIX = bytes.fromhex("302a300506032b6570032100")


class RecoveryError(Exception):
    pass


def unique(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise RecoveryError("duplicate_json_key")
        result[k] = v
    return result


def decode(raw):
    if len(raw) > MAX:
        raise RecoveryError("metadata_too_large")
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(RecoveryError("nonfinite_json")))
    except (ValueError, UnicodeError) as exc:
        raise RecoveryError("invalid_json") from exc


def canonical(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True, allow_nan=False).encode()
    if len(raw) > MAX:
        raise RecoveryError("metadata_too_large")
    return raw


def read(path, max_size=MAX):
    with open(path, "rb") as f:
        raw = f.read(max_size + 1)
    if not raw or len(raw) > max_size:
        raise RecoveryError("invalid_file_size")
    return raw


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def run(*args):
    try:
        return subprocess.check_output(["openssl", *args], stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RecoveryError("signature_command_failed") from exc


def public_der(key, private=False):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "key"
        p.write_bytes(read(key, 4096))
        der = run("pkey", *([] if private else ["-pubin"]), "-in", str(p), "-pubout", "-outform", "DER")
    if len(der) != 44 or not der.startswith(PREFIX):
        raise RecoveryError("expected_ed25519_key")
    return der


def fingerprint(key, private=False):
    return hashlib.sha256(public_der(key, private)).hexdigest()


def sign_bytes(raw, key):
    public_der(key, True)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        (p / "message").write_bytes(raw)
        (p / "key").write_bytes(read(key, 4096))
        sig = run("pkeyutl", "-sign", "-inkey", str(p / "key"), "-rawin", "-in", str(p / "message"))
    if len(sig) != 64:
        raise RecoveryError("invalid_signature_length")
    return sig


def verify_bytes(raw, sig, key, private=False):
    der = public_der(key, private)
    # Verify using only the derived public key, even for local witness state.
    pem = b'-----BEGIN PUBLIC KEY-----\n' + base64.b64encode(der) + b'\n-----END PUBLIC KEY-----\n'
    if len(sig) != 64:
        raise RecoveryError("invalid_signature_length")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        for name, content in (("message", raw), ("sig", sig), ("key", pem)):
            (p / name).write_bytes(content)
        run("pkeyutl", "-verify", "-pubin", "-inkey", str(p / "key"), "-rawin",
            "-in", str(p / "message"), "-sigfile", str(p / "sig"))


def sign(payload, key):
    return {"payload": payload, "signature": base64.b64encode(sign_bytes(canonical(payload), key)).decode()}


def verify(envelope, key, private=False):
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature"}:
        raise RecoveryError("invalid_envelope")
    try:
        sig = base64.b64decode(envelope["signature"], validate=True)
    except (ValueError, TypeError) as exc:
        raise RecoveryError("invalid_signature_encoding") from exc
    verify_bytes(canonical(envelope["payload"]), sig, key, private)
    return envelope["payload"]
