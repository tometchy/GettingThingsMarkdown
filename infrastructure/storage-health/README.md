# GTMD storage-node health (opt-in host service)

This is the first hardware-observability increment of MVP §17.2. One shared
probe per physical storage host, no extra tenant container or Kubernetes
credentials. It **does not quarantine a disk or modify a PV/PVC**.

Provision on selected physical storage nodes through approved IaC/host
bootstrap (not a tenant pod). Install Python 3 and smartmontools from the OS
package manager. Install server/integrity/disk_health.py as root-owned
/usr/local/lib/gtmd/disk_health.py, the two systemd unit files in
/etc/systemd/system, and a root-owned mode 0600 /etc/gtmd/disk-health.json:

    {"disks":[{"byId":"ata-EXAMPLE_SERIAL","serial":"EXAMPLE_SERIAL"}]}

Use the actual whole-drive /dev/disk/by-id basename and exact smartctl JSON
serial; explicitly inventory every tenant storage disk and system disk
supported by the probe. Replace the inventory only after hardware identity
verification. SATA/ATA and NVMe implemented; SAS, RAID virtual devices and
other unsupported telemetry are UNKNOWN, never silently healthy. Host agent
needs root SMART ioctl access; the unit restricts networking and filesystem
writes but **is not a security sandbox against compromised root code**. Do not
mount host /dev into untrusted tenant containers.

Once approved, systemctl daemon-reload and systemctl enable --now
gtmd-disk-health.timer. This is not executed by CI. The service checks up to
four disks concurrently, each with a 20-second timeout; a 180-second service
timeout and 60-second delay after completion bound typical 18-disk runs.
A disappeared, substituted, unreadable, unsupported or unresponsive disk is
UNKNOWN, a current SMART fail is FAILED; historical error log entries and
nonzero media defect counters are conservatively SUSPECT. ATA attribute raw
values and thresholds vary by vendor; validate and tune on actual models.
SMART PASS is not evidence of Git or filesystem integrity.

Connect the existing private node_exporter to
--collector.textfile.directory=/var/lib/gtmd-disk-health (if already configured,
integrate into its existing textfile path; never overwrite another collector's
files). Ensure node_exporter can read the metrics file, but not
disk-health.json (0600, contains hardware serials for tickets). Set target
label gtmd_storage_node="true" for *every expected storage node*, including
failed ones, in a persistent inventory; load prometheus-rules.yaml into both
independent monitoring installations and route alerts to the private
observability channel. Target disappearance from service discovery entirely
requires independent expected-node inventory reconciliation, still TODO.
Do not publish node_exporter or logs to the public internet.

No customer filenames, secrets, Git OIDs or hardware serials are exported in
metrics/logs. A hashed serial is a stable opaque disk identifier, not a secret.
The local private JSON is intended for a future disk-to-PV controller and
hardware ticket adapter. If config becomes invalid, metrics become explicitly
unknown; the old private JSON is retained only as historical diagnostic data,
not current authorization. Do not act on it without fresh successful telemetry.

Pending: independent Git scan metrics and working-tree/physical scrub,
kernel journal I/O error events, persistent expected node+PV mapping, operator
quarantine/fencing, source authentication and automatic volume rebuild,
provider ticket submission, failover and real hardware/throughput validation.
No paid infrastructure or cluster modifications are performed by this change.
