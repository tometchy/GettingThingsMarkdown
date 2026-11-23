# Revenue-safe storage architecture — November 2026

**Decision:** Do not purchase two full-price Scaleway Block Storage PVCs per tenant.
Keep exactly two long-running per-tenant pods (A: encrypted Syncthing + Git;
B: Gitea + SQLite), but move the *encrypted Git LFS file contents* from B's
second large block volume to an S3-compatible object store. B keeps only Git
references/pointers, SQLite and local metadata. A keeps a real filesystem.
Object storage is not mounted as a POSIX filesystem.

**Provider direction:** Hetzner affordable HDD-backed workers (auction servers
or suitable self-hosted K3s nodes; dynamically available) plus Hetzner S3
Object Storage for LFS and separately budgeted immutable backups. Rootless
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
- Configure object locking **COMPLIANCE mode, 30 days** on backup buckets
  created with Object Lock enabled; governance can be bypassed.
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
second 200 GiB block disk for Plus. The Sync PVC remains 200 GiB (or the
development size) and must use a cost-efficient enforceable HDD-backed storage
class. This does not imply history metadata will never exceed 8 GiB; reserve
space, monitor and resize when needed.
