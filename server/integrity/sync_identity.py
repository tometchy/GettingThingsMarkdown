#!/usr/bin/env python3
"""Shared read-only Syncthing configuration and device identity checks."""
import re
import subprocess
import xml.etree.ElementTree as ET
from recovery_crypto import RecoveryError

DEVICE_ID = re.compile(r"[A-Z2-7]{7}(?:-[A-Z2-7]{7}){7}\Z")


def require(condition, reason):
    if not condition:
        raise RecoveryError(reason)


def validate_config(raw, cfg):
    xml = raw.decode("utf-8", errors="strict")
    require("<!DOCTYPE" not in xml.upper() and "<!ENTITY" not in xml.upper(),
            "unsupported_identity_xml")
    root = ET.fromstring(xml)
    require(root.tag == "configuration", "invalid_syncthing_configuration")
    folders = root.findall("folder")
    require(len(folders) == 1 and folders[0].get("id") == cfg["folderId"]
            and folders[0].get("type") == "receiveencrypted"
            and folders[0].get("path") == "/data/files", "unexpected_syncthing_folder")
    require(sum(d.get("id") == cfg["deviceId"] for d in root.findall("device")) == 1,
            "device_id_not_in_config")
    require(not any((x.text or "").strip() for x in root.iter("encryptionPassword")),
            "unexpected_plaintext_folder_secret")


def verify_device(home, cfg, timeout=30):
    def command(args):
        try:
            return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, check=True, timeout=timeout).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise RecoveryError("syncthing_identity_command_failed") from exc
    # Check the key pair first: never invoke the Syncthing CLI with a
    # missing or invalid pair that it could decide to regenerate.
    cert = command(["openssl", "x509", "-in", str(home/"cert.pem"), "-pubkey", "-noout"])
    key = command(["openssl", "pkey", "-in", str(home/"key.pem"), "-pubout"])
    require(cert == key, "restored_certificate_key_mismatch")
    actual = command(["syncthing", "device-id", "--home="+str(home)]).decode("ascii").strip()
    require(actual == cfg["deviceId"], "restored_device_id_mismatch")
