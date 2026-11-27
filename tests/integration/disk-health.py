import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("disk_health",
    Path(__file__).resolve().parents[2] / "server/integrity/disk_health.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class DiskHealthTests(unittest.TestCase):
    def ata(self, **values):
        return {"serial_number": "DRIVE1", "smart_status": {"passed": True},
                "ata_smart_attributes": {"table": [
                    {"id": i, "raw": {"value": values.get(k, 0)}}
                    for i, k in h.ATA_ATTRS.items()]}}

    def nvme(self, **values):
        v = dict(critical_warning=0, media_errors=0, percentage_used=1,
                 available_spare=100, available_spare_threshold=10)
        v.update(values)
        return {"serial_number": "DRIVE1", "smart_status": {"passed": True},
                "nvme_smart_health_information_log": v}

    def assess(self, data, code=0):
        return h.assess(data, code, "DRIVE1")[0]

    def test_healthy(self):
        self.assertEqual(self.assess(self.ata()), "healthy")
        self.assertEqual(self.assess(self.nvme()), "healthy")

    def test_degrading_media(self):
        for key in ("reallocated", "reported_uncorrectable", "pending", "offline_uncorrectable"):
            self.assertEqual(self.assess(self.ata(**{key: 1})), "suspect")
        self.assertEqual(self.assess(self.nvme(media_errors=3)), "suspect")
        self.assertEqual(self.assess(self.nvme(percentage_used=100)), "suspect")

    def test_failed_media(self):
        self.assertEqual(self.assess(self.ata(), 8), "failed")
        self.assertEqual(self.assess(self.ata(), 16), "failed")
        self.assertEqual(self.assess(self.ata(), 12), "failed")
        self.assertEqual(self.assess(self.nvme(critical_warning=4)), "failed")
        self.assertEqual(self.assess(self.nvme(available_spare=1)), "failed")
        d = self.ata()
        d["smart_status"]["passed"] = False
        self.assertEqual(self.assess(d), "failed")

    def test_historical_log_not_definite_failure(self):
        self.assertEqual(self.assess(self.ata(), 64), "suspect")
        self.assertEqual(self.assess(self.ata(), 128), "suspect")

    def test_unknown_not_healthy(self):
        self.assertEqual(self.assess(self.ata(), 2), "unknown")
        self.assertEqual(self.assess(self.ata(pending="bad")), "unknown")
        d = self.ata()
        del d["ata_smart_attributes"]
        self.assertEqual(self.assess(d), "unknown")
        self.assertEqual(self.assess({"serial_number": "OTHER"}, 0), "unknown")
        self.assertEqual(self.assess(self.nvme(media_errors=None)), "unknown")
        self.assertEqual(self.assess(self.ata(), -9), "unknown")

    def test_temperature_and_interface_counter(self):
        d = self.ata(interface_crc=42)
        d["temperature"] = {"current": 47}
        state, _, counters = h.assess(d, 0, "DRIVE1")
        self.assertEqual(state, "healthy")
        self.assertEqual(counters["temperature_celsius"], 47)
        self.assertEqual(counters["interface_crc"], 42)

    def test_missing_device(self):
        d = {"byId": "gtmd-ci-impossible-device-123", "serial": "DRIVE1", "id": "a"}
        self.assertEqual(h.probe(d)["state"], "unknown")

    def test_inventory_and_metrics(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root)
            c = p / "inventory.json"
            c.write_text(json.dumps({"disks": [
                {"byId": "ata-EXAMPLE-DRIVE1", "serial": "DRIVE1"},
                {"byId": "ata-EXAMPLE-DRIVE2", "serial": "DRIVE2"}]}))
            def fake(d):
                return {"id": d["id"], "state": "healthy" if d["serial"] == "DRIVE1" else "unknown",
                        "counters": {}, "reasons": [], "checkedUnix": 100}
            self.assertTrue(h.collect(c, p, fake))
            text = (p / "gtmd_disk_health.prom").read_text()
            self.assertIn("gtmd_disk_health_expected_disks 2", text)
            self.assertIn('state="unknown"} 1', text)
            self.assertNotIn("DRIVE", text)
            self.assertEqual((p / "disk-health.json").stat().st_mode & 0o777, 0o600)
            c.write_text('{"disks":[]}')
            self.assertFalse(h.collect(c, p, fake))
            self.assertIn("gtmd_disk_health_inventory_ok 0", (p / "gtmd_disk_health.prom").read_text())

    def test_invalid_inventory(self):
        with tempfile.TemporaryDirectory() as root:
            c = Path(root) / "inventory.json"
            for device in ("../../sda", "ata-disk-part1", ".."):
                c.write_text(json.dumps({"disks": [{"byId": device, "serial": "S"}]}))
                with self.assertRaises(ValueError):
                    h.inventory(c)


if __name__ == "__main__":
    unittest.main()
