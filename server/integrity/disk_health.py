#!/usr/bin/env python3
"""Read-only host disk-health probe. No disk writes, eviction or provider API."""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time

ID = re.compile(r"[A-Za-z0-9_.:+-]{1,200}\Z")
LEVELS = {"healthy": 0, "suspect": 1, "failed": 2, "unknown": 3}
ATA_ATTRS = {5: "reallocated", 187: "reported_uncorrectable",
             197: "pending", 198: "offline_uncorrectable", 199: "interface_crc"}
TIMEOUT = 20


def number(value):
    # Never convert malformed telemetry to zero.
    return value if type(value) is int and value >= 0 else None


def assess(data, exit_code, expected_serial):
    """Evaluate smartctl JSON/exit bitmask. Historic logs are not proof of failure."""
    if not isinstance(data, dict) or not 0 <= exit_code <= 255:
        return "unknown", ["invalid_telemetry"], {}
    serial = data.get("serial_number")
    if serial != expected_serial:
        return "unknown", ["identity_mismatch"], {}
    if exit_code & 7:
        # SMART can report both incomplete commands and a known failed health bit.
        if exit_code & 24:
            return "failed", ["smart_command_incomplete", "smart_health_failed"], {}
        return "unknown", ["smart_command_incomplete"], {}
    reasons = []
    counters = {}
    severity = "healthy"
    def flag(level, reason):
        nonlocal severity
        reasons.append(reason)
        if LEVELS[level] > LEVELS[severity]:
            severity = level
    status = data.get("smart_status")
    passed = status.get("passed") if isinstance(status, dict) else None
    if passed is False or exit_code & 8:
        flag("failed", "smart_health_failed")
    if exit_code & 16:
        flag("failed", "current_prefail_threshold")
    if exit_code & 32:
        flag("suspect", "past_threshold")
    if exit_code & 64:
        flag("suspect", "error_log_present")
    if exit_code & 128:
        flag("suspect", "self_test_error_log_present")
    ata = data.get("ata_smart_attributes")
    nvme = data.get("nvme_smart_health_information_log")
    # SATA/ATA and NVMe currently supported. Do not report unsupported SAS
    # healthy merely because smartctl returned a generic PASS.
    if isinstance(ata, dict) and isinstance(ata.get("table"), list):
        recognized = set()
        for item in ata["table"]:
            if not isinstance(item, dict) or item.get("id") not in ATA_ATTRS:
                continue
            key = ATA_ATTRS[item["id"]]
            raw = item.get("raw")
            val = number(raw.get("value")) if isinstance(raw, dict) else None
            if val is None:
                flag("unknown", "malformed_ata_counter")
                continue
            recognized.add(key)
            counters[key] = val
            if val and key in ("pending", "offline_uncorrectable", "reported_uncorrectable"):
                flag("suspect", key)
            elif val and key == "reallocated":
                # Conservative until thresholds/trends validated per drive model.
                flag("suspect", key)
        if not {"reallocated", "pending", "offline_uncorrectable"} <= recognized:
            flag("unknown", "ata_media_counters_missing")
    elif isinstance(nvme, dict):
        for key in ("critical_warning", "media_errors", "percentage_used",
                    "available_spare", "available_spare_threshold"):
            val = number(nvme.get(key))
            if val is None:
                flag("unknown", "nvme_counter_missing")
            else:
                counters[key] = val
        if counters.get("critical_warning", 0):
            flag("failed", "nvme_critical_warning")
        if counters.get("media_errors", 0):
            flag("suspect", "nvme_media_errors")
        if counters.get("percentage_used", 0) >= 100:
            flag("suspect", "nvme_endurance_exhausted")
        if ("available_spare" in counters and "available_spare_threshold" in counters
                and counters["available_spare"] < counters["available_spare_threshold"]):
            flag("failed", "nvme_spare_below_threshold")
    else:
        flag("unknown", "unsupported_media_telemetry")
    temperature = data.get("temperature")
    if isinstance(temperature, dict):
        val = number(temperature.get("current"))
        if val is not None:
            counters["temperature_celsius"] = val
    if passed is not True and passed is not False:
        flag("unknown", "smart_status_missing")
    # An explicit observed failure must not be hidden by incomplete fields.
    if "smart_health_failed" in reasons or "current_prefail_threshold" in reasons or any(
            x in reasons for x in ("nvme_critical_warning", "nvme_spare_below_threshold")):
        severity = "failed"
    return severity, sorted(set(reasons)), counters


def inventory(path):
    config = json.loads(Path(path).read_text())
    if not isinstance(config, dict) or set(config) != {"disks"} or not isinstance(config["disks"], list) or not config["disks"]:
        raise ValueError("empty_or_invalid_inventory")
    disks = []
    seen = set()
    for item in config["disks"]:
        if not isinstance(item, dict) or set(item) != {"byId", "serial"}:
            raise ValueError("invalid_disk")
        by_id, serial = item["byId"], item["serial"]
        if not isinstance(by_id, str) or not ID.fullmatch(by_id) or by_id in (".", "..") or re.search(r"-part[0-9]+$", by_id):
            raise ValueError("invalid_device_identity")
        if not isinstance(serial, str) or not ID.fullmatch(serial) or serial in seen:
            raise ValueError("invalid_or_duplicate_serial")
        seen.add(serial)
        disks.append({"byId": by_id, "serial": serial,
                      "id": hashlib.sha256(serial.encode()).hexdigest()[:24]})
    if len({d["byId"] for d in disks}) != len(disks):
        raise ValueError("duplicate_device")
    return disks


def probe(disk):
    started = time.time()
    try:
        path = Path("/dev/disk/by-id") / disk["byId"]
        if not path.is_symlink():
            raise OSError("device_missing")
        target = path.resolve(strict=True)
        if target.parent != Path("/dev") or not stat.S_ISBLK(target.stat().st_mode):
            raise OSError("not_block_device")
        # Whole physical disk only, never a partition or a device-mapper volume.
        sys = Path("/sys/class/block") / target.name
        if (sys / "partition").exists() or target.name.startswith(("dm-", "loop", "md", "zram")):
            raise OSError("not_physical_disk")
        result = subprocess.run(["smartctl", "-a", "-j", str(path)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=TIMEOUT, check=False)
        status, reasons, counters = assess(json.loads(result.stdout),
                                          result.returncode, disk["serial"])
        # Linux SCSI error counter is optional and cumulative. Missing is not zero.
        try:
            errors = int((sys / "device/ioerr_cnt").read_text().strip(), 0)
            if errors >= 0:
                counters["kernel_io_errors"] = errors
                if errors and status == "healthy":
                    status = "suspect"
                    reasons.append("kernel_io_errors")
        except (OSError, ValueError):
            pass
    except subprocess.TimeoutExpired:
        status, reasons, counters = "unknown", ["smart_timeout"], {}
    except (OSError, ValueError, TypeError):
        status, reasons, counters = "unknown", ["device_or_telemetry_unavailable"], {}
    return {"id": disk["id"], "state": status, "reasons": reasons,
            "counters": counters, "checkedUnix": time.time(),
            "durationSeconds": round(time.time() - started, 3)}


def atomic_write(path, text, mode):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as tmp:
        name = tmp.name
        try:
            os.fchmod(tmp.fileno(), mode)
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
    try:
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def metrics(results, now, ok):
    lines = ["# TYPE gtmd_disk_health_inventory_ok gauge",
             f"gtmd_disk_health_inventory_ok {int(ok)}",
             "# TYPE gtmd_disk_health_last_poll_unixtime gauge",
             f"gtmd_disk_health_last_poll_unixtime {now:.3f}",
             "# TYPE gtmd_disk_health_expected_disks gauge",
             f"gtmd_disk_health_expected_disks {len(results)}",
             "# TYPE gtmd_disk_health_state gauge"]
    for d in results:
        for state in LEVELS:
            lines.append(f'gtmd_disk_health_state{{disk="{d["id"]}",state="{state}"}} {int(state == d["state"])}')
    lines.append("# TYPE gtmd_disk_health_checked_unixtime gauge")
    for d in results:
        lines.append(f'gtmd_disk_health_checked_unixtime{{disk="{d["id"]}"}} {d["checkedUnix"]:.3f}')
    lines.append("# TYPE gtmd_disk_health_counter gauge")
    for d in results:
        for key, val in sorted(d["counters"].items()):
            lines.append(f'gtmd_disk_health_counter{{disk="{d["id"]}",counter="{key}"}} {val}')
    return "\n".join(lines) + "\n"


def collect(config, state_dir, poll=probe):
    """One bounded poll; absent disks remain in the expected inventory."""
    try:
        disks = inventory(config)
    except (OSError, ValueError, TypeError):
        # Replace previous green metrics with unknown inventory, never keep stale success.
        atomic_write(Path(state_dir) / "gtmd_disk_health.prom", metrics([], time.time(), False), 0o644)
        atomic_write(Path(state_dir) / "disk-health.json",
                     json.dumps({"checkedUnix": time.time(), "inventoryOk": False, "disks": []}) + "\n", 0o600)
        print(json.dumps({"event": "gtmd_disk_health", "state": "unknown",
                          "reason": "inventory_unavailable"}), flush=True)
        return False
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(poll, disks))
    now = time.time()
    atomic_write(Path(state_dir) / "gtmd_disk_health.prom", metrics(results, now, True), 0o644)
    # Private mapping for the hardware operator; not exported to Prometheus.
    private = [{"byId": d["byId"], "serial": d["serial"], **v}
               for d, v in zip(disks, results)]
    atomic_write(Path(state_dir) / "disk-health.json",
                 json.dumps({"checkedUnix": now, "inventoryOk": True, "disks": private}) + "\n", 0o600)
    for v in results:
        print(json.dumps({"event": "gtmd_disk_health", **v}, separators=(",", ":")), flush=True)
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inventory", required=True)
    p.add_argument("--state-dir", required=True)
    args = p.parse_args()
    return 0 if collect(args.inventory, args.state_dir) else 1


if __name__ == "__main__":
    raise SystemExit(main())
