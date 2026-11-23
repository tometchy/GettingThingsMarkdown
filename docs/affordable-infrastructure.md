# Revenue-safe storage architecture — November 2026

**Decision:** Do not purchase two full-price Scaleway Block Storage PVCs per tenant.
Keep exactly two long-running per-tenant pods (A: encrypted Syncthing + Git;
B: Gitea + SQLite), but move the *encrypted Git LFS file contents* from B's
second large block volume to an S3-compatible object store. B keeps only Git
references/pointers, SQLite and local metadata. A keeps a real filesystem.
Object storage is not mounted as a POSIX filesystem.

**Provider direction:** Hetzner low-cost HDD Sync workers and independently
controlled OVHcloud worker(s) for Gitea/History; Backblaze B2 as potential
third-provider Git LFS bucket. Cross-cloud networking, cluster and fencing
are not deployed, and the old Hetzner-only setup is not redundant. Rootless
Podman + kind remains the local POC. Existing Scaleway Terraform is kept as
an alternative but is not the chosen paid production target.

## Prices and assumptions (not a quote)

| Service | Referenced list price | Notes |
|---|---:|---|
| Scaleway SSD 5K | €0.000130/GB/hour | 2x 200 GiB is prohibitive |
| Hetzner Storage Box BX31 | €20.80/10 TB/month | Cheap for remote backups, **not SQLite** |
| Hetzner Object Storage | €6.49/month base (1 TB incl.) | Subsequent pooled usage billed per TB-hour; assumed €6.49/TB equivalent |
| Backblaze B2 | $6.95/TB/month | Optional independent Object Lock provider |
| Hetzner auction HDD server | €132/month for sample 4 × 16 TB | Varies by supply; 32 TB *assumed usable* after RAID/redundancy |

Provider references:
- https://www.scaleway.com/en/pricing/storage/
- https://www.hetzner.com/storage/storage-box/
- https://www.hetzner.com/storage/object-storage/
- https://docs.hetzner.com/storage/object-storage/howto-protect-objects/protect-object-lock-retention/
- https://www.backblaze.com/cloud-storage/pricing

The storage economics is **contingent on load**, not guaranteed. The model
uses a 75%-filled 32 TB usable HDD worker pool, 1.5× archive/version growth,
a 1.5× backup budget, €0.50 per-tenant shared compute, €0.80 support/ops,
23% customer VAT, an assumed 1.5% + €0.25 processing fee. Actual VAT,
payment charges, churn, traffic, failure domains and refunds will differ.
Run `python3 scripts/cost_model.py` and vary utilization/multiplier.
The previous €4 gross 200 GiB plan loses money under these assumptions;
a **provisional** €9.99 gross Plus price creates a buffer, but Stripe prices
must not be changed without verification. Other provisional prices:
Basic €3.99, Pro €39.99 gross. They're candidate business prices, NOT
live charges or a promise of profitability. Stress-test under worst-case
usage before approving commercial launch.

## Required production isolation

Each tenant's Gitea must receive keys restricted to an isolated bucket.
The node's Syncthing never receives S3 secrets, and the user-supplied
encryption password never reaches Gitea or the control plane.
Hetzner currently limits the number of buckets per account (see provider
docs); **do not** simply give all tenant pods a global key. At scale, a
dedicated shared broker that authorizes per-tenant prefixes or separate
accounts/bucket sharding is required.

## Non-negotiable safety gates

- Verify native decrypt after both live files and Git LFS history are restored.
- Use a real HDD-backed Kubernetes CSI provisioner with enforceable quotas
  and anti-affinity across *independent physical hosts*, not kind local-path
  for paid accounts.
- **Leave optional 30-day WORM disabled when implementing the requested 7-day
  deletion/undo policy**: long retention conflicts with the purge promise.
  Future protection must apply to fresh quarantine objects with a bounded
  expiry rather than depend on a lock set a year ago.
- Keep metadata backup independently of LFS objects and Syncthing identity.
- Restore-test against node failure, object store outage, and a revoked key.
- Preserve two isolated long-running containers per tenant; helper init
  containers are ephemeral and terminate before the Sync pod starts.
- Do not claim full 200 GiB profitability until workloads and bills confirm
  the model; S3 minimum object billing size can inflate costs for tiny files.
- Do not deploy paid cloud resources without explicit operational approval.

## Production Helm flags

The chart defaults `encryptedArchive.lfsEnabled=false` and
`encryptedArchive.wormBackupEnabled=false`, so local kind needs no remote
storage credentials. Enabling both in real production also requires
**per-tenant** secrets `gtm-lfs-credentials` and `gtm-worm-credentials`, and
an S3 bucket created with default 30-day COMPLIANCE Object Lock.

When Git LFS is enabled the independent History PVC is 8 GiB rather than a
second 200 GiB block disk for Plus. The Sync PVC now reserves 2× the customer live quota (e.g. 400 GiB
for a legacy 200 GiB Plus account, 150 GiB for a new 75 GiB Plus account,
or 2× the development size) and must use a cost-efficient enforceable HDD-backed storage
class. This does not imply history metadata will never exceed 8 GiB; reserve
space, monitor and resize when needed.

## Backup operational policy

When `encryptedArchive.wormBackupEnabled=true`, the tenant History process
runs `backup-loop.sh` inside the **existing History container**. Every 6 hours
it snapshots a consistent SQLite DB and a Git bundle of the encrypted history,
symmetrically encrypts the snapshot using an operator-held recovery passphrase,
uploads it with explicit 31-day COMPLIANCE retention and verifies the resulting
object's lock. It then incrementally `rclone copy --immutable` copies opaque
Git LFS blobs into the same WORM destination. It never deletes any WORM object.

The operator must pre-create an **Object Lock-enabled** bucket with default
COMPLIANCE >=30 days, and a distinct backup access key with the narrowest
possible permissions for that tenant. Create Kubernetes Secret
`gtm-worm-credentials` in that tenant namespace with keys
`endpoint-url`, `bucket`, `region`, `access-key-id`,
`secret-access-key`, `backup-passphrase`. Keep the passphrase separately,
offline: losing it makes Gitea metadata snapshots unusable. The API never
returns backup credentials or plaintext.

The backup worker stops rather than silently downgrade COMPLIANCE to
GOVERNANCE. Alert on repeated failures, storage class outages, or a bucket
with incorrect retention. Hourly/6h snapshots can grow rapidly under the
30-day minimum retention and must be included in cost testing. No real S3
bucket has been provisioned or encrypted restore performed against a paid
provider yet.

The WORM snapshot contains **only History metadata and encrypted Git LFS
blobs**. It does not yet contain the Syncthing server's unique device identity
or a transactionally consistent backup of the Control Plane account database.
Both are release blockers; no SLA should be offered until disaster recovery
can recreate the tenant's identity with a tested point-in-time restore.

## Shared buckets at scale

Gitea 1.27 supports `[lfs] MINIO_BASE_PATH` independently of its
`[storage]` settings. The manifest explicitly prefixes LFS objects under
`tenants/gtm-<account-guid>/lfs/` and configures the backup worker with the
same prefix. **A prefix alone does not enforce security**: the provider must
issue application keys restricted to that prefix and tenant. Hetzner allows
bucket-scoped access controls, but its bucket limit makes one bucket per user
non-scalable beyond the configured account allowance. Backblaze B2 supports
S3-compatible keys scoped to *bucket and file-name prefix* and can be used
for the active S3 backend with similar unit economics if that key isolation is
needed at scale. A tenant's Git LFS credentials must not be able to read other
tenants' object prefixes.

Sources:
- https://docs.gitea.com/1.27/administration/config-cheat-sheet/
- https://www.backblaze.com/docs/cloud-storage-s3-compatible-app-keys
- https://docs.hetzner.com/storage/object-storage/faq/s3-credentials/

Do not mix per-tenant keys with a global S3 key as a fallback. On a shared
bucket, a separate bucket-scoped key is insufficient to enforce isolation.

## CPU, node-count and minimum customer scale (critical correction)

The first per-user storage-only cost model was **too optimistic for a SaaS**
that permanently keeps two Kubernetes pods per tenant. It amortized HDD
capacity at 75% utilization but ignored the node count required by Kubernetes
CPU requests. A second, more conservative, full-fleet scenario is implemented
in `StorageEconomics.fleet()`. It considers:

- One Sync and one History pod per user; **100m CPU and 256MiB RAM requests each**.
- Illustrative 8-core, 64GiB RAM, €132/month dedicated HDD worker with 70%
  schedulable CPU/RAM and 32TB usable HDD (RAID); minimum two workers.
- At most 75% of usable HDD capacity consumed by live files; history stored
  separately at 1.5× live bytes; backup Boxes rounded up to full units.
- Additional €30 shared Control Plane monthly, support/ops €0.80 per user,
  payment processing, VAT and S3 history costs; no live price guarantee.

At **150 full Plus (200GiB) customers** approximately six 8-core HDD nodes
are required by the CPU requests alone, even though disk space could fit on
fewer. Under these assumptions **€9.99 gross is no longer profitable**, while
€15.99 gross approaches a 25% contribution margin. At **10 customers** even
€15.99 loses money because two dedicated servers are required. Run the tool
for scenarios at 10/50/100/150 tenants; the outcome changes substantially
with node CPU capacity and real utilization. It is **not correct to sell
200GiB for €9.99 on the strength of the original per-TB model**.

No prices have been changed in Stripe. Only an actual load test, hardware
quote and provider invoice can establish a defensible commercial price.

## Current offer replaces earlier illustrations

The scenarios elsewhere in this document mentioning €3.99/€9.99/€39.99
and 15/200/1024 GiB are historical comparisons. They **are not** public
plans and must not be copied into Stripe. The new proposed actual catalog is
10 GiB at $1.99/mo, 75 GiB at $5.99/mo, and 500 GiB at $24.99/mo;
annually $17.88/$59.88/$239.88. Refer to `docs/pricing.md`.

The `mixed_fleet` scenario uses a plausible but unverified mix of
20% Mini, 60% Plus, 20% Pro, two pod requests per tenant and indicative
USD-to-EUR rate 0.9093 as at November 23, 2026. Under these assumptions,
50 customers lose money because two always-on HDD workers and shared
control-plane dominate. At 100 clients contribution is only slightly positive;
at 500 clients simulated gross contribution margin is about 19%. This is
**before salaries, taxes on profit, unexpected bandwidth, abuse, refunds,
contract downtime and full WORM retention costs**. Success in a calculation
is not proof of sustainable SaaS margins. Perform physical workload and
occupancy tests before quoting profits or charging users.
