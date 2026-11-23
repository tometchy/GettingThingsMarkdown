# Cross-cloud recovery architecture and uncovered attack paths

Status: **architecture proposal / partial security hardening only**. No vendor
resources or dual-provider cluster were provisioned. Enabling node placement
does **not** enable automatic failover.

## Cost and provider choice (23 November 2026)

- Sync + native encrypted Syncthing data: Hetzner HDD-backed worker **only if
  backed by a quota-enforcing CSI/storage class**.
- History/Gitea: OVHcloud VPS-1 2027, advertised in Poland **from PLN 16.32
  net/month** for 2 vCore, 4 GB and 40 GB NVMe; that is a whole shared
  worker, not a per-tenant disk allowance or an HA guarantee.
  https://www.ovhcloud.com/pl/vps/cheap-vps/
- Alternative Scaleway DEV1-S around **EUR 6.55/month** (2 vCPU, 2 GB,
  priced separately from extras).
  https://www.scaleway.com/en/pricing/virtual-instances/
- VPS-1 can hold only a handful of 8 GiB History PVCs after filesystem and
  runtime overhead. Git/LFS metadata, cross-cloud transfers, S3 costs,
  offsite backups, cluster control-plane quorum and HA reservations can
  make Mini/Plus/Pro prices unprofitable. No validated capacity model yet.
- Provider separation is meaningful only with distinct legal/control accounts,
  credentials, network and durable volumes; two nodes in a single provider
  are not two cloud failure domains.

## Explicit write boundary

Only the tenant's Sync pod has the Gitea `sync` account with Git write access.
The Control Plane has the `reader` collaborator only. Gitea's `archive`
administrator is used *locally for initial repository and permissions setup*.
No public ingress for History. NetworkPolicies allow History's HTTPS port only
from that tenant's Sync pod and a labeled Control Plane pod, and Syncthing
administration only from Control Plane. The `sync` writer cannot force-push or
delete Git refs (Git receive protection), pending a separately authorized
history-erasure protocol.

This is **not** a read-only Gitea binary or immutable storage. Root in the
History host, a compromised Kubernetes controller (which can read Secrets),
the Gitea administrator password or a malicious Gitea process can still write
repository data. The NetworkPolicy requires a compatible CNI and may not be
effective in the default kind installation. No untrusted user's login is
provisioned to the internal Gitea.

## Deletion semantics — not yet implemented

1. The trusted client resolves a human-readable filename to its *opaque,
   encrypted* history identity. The server cannot parse the plaintext name.
2. Record a durable, access-controlled deletion intent with an idempotency ID
   and an authoritative expiry date **7 days from request**, not 7 days from
   the file's original upload.
3. Freeze writes and snapshot the old history refs + all referenced encrypted
   LFS objects **before** rewriting. The recovery bytes must live in a
   separately protected, isolated store or they are deletable by compromised
   History administrators.
4. Rewrite normal history only through a privileged, audited maintenance
   operation in Sync; keep an untamperable recovery ref and support undo.
   Default receive.denyNonFastForwards=true therefore correctly blocks
   casual rewrites until this deliberately separate operation exists.
5. Decrease logical billed quota only once there is a committed removal.
   Separate anti-abuse recovery budget is required if freed immediately.
6. After seven days, safely expire recovery refs, run Git GC and remove
   unreferenced LFS objects and protected backup versions after retention.
   Generic Git GC does not delete Git LFS S3 objects by itself.
7. Never advertise permanent deletion while unrelated snapshots/replicas
   preserve the old object; publish precise purge/RPO guarantees.

Existing optional 30-day WORM backup does not satisfy these deletion rules
and must remain disabled. An old 7-day object lock **does not** protect a
one-year-old object. Protection must be continuous (rolling) or newly written
at the time a delete intent is recorded. An Object Lock compliant backup can
be held for an explicit 7 days, but only versions **actually under an active
retention** are immutable.

## Single-cloud loss — required fail-closed restoration contract

Architecture must provide **two complete encrypted history replicas**,
including ordinary Git objects *and* offloaded Git LFS. Current implementation
does not: `git lfs prune --verify-remote` removes local A cache after the B
remote accepted it, and B's LFS lives in a third object storage service.
The plaintext-free live Syncthing ciphertext on A is **not** a substitute
for all old LFS snapshots. No automatic recovery can be safe until this is fixed.

A. If **Hetzner/A fails**, freeze the shared control-plane write entitlement.
   Verify OVH/B has a complete cryptographically checked snapshot: Git refs,
   all LFS object IDs, server Syncthing identity/config, Git metadata and
   account preferences, and independently recoverable object-store keys.
   Fence the dead A node through Hetzner's API or a trusted quorum/witness,
   *then* create a replacement Sync pod on OVH from the verified replica.
   Its Syncthing Device ID **must not change**. Only after stable rehydration
   and peer checks permit uploads again.

B. If **OVH/B fails**, do not mount an orphaned OVH PVC on Hetzner. Verify
   Hetzner/A has every needed Git/LFS byte and a consistent replica of Gitea
   config/SQLite. Fence B. Only then provision History on Hetzner from the
   verified snapshot. The Sync worker may not push to a partially restored B.

C. If the two sites can both run but **inter-site link breaks**, disable
   automatic writes or use independent quorum to select a *single* writer;
   otherwise split-brain and undetected history loss are inevitable. Two
   sites by themselves cannot safely elect a winner in all partitions.

D. If the **Control Plane SQLite / Stripe / K8s API** disappears, neither
   remaining tenant pod reconstructs billing entitlement or user login today.
   This needs encrypted, frequent, versioned and tested account snapshots
   on both providers, an external witness/lease and documented bootstrap
   recovery credentials. Two copies of user settings must be kept in sync
   transactionally or with clear sequence numbers/conflict resolution.

E. If one **object-storage region** fails, both Git clones may still contain
   only LFS pointers. Need two independently accessible LFS stores with
   retrieval tests, checksums and repair logic. Neither a Git branch nor
   Object Lock alone provides data-plane redundancy.

### Safety conditions for future automatic failover

- Fenced old primary verified **before** promoting another.
- Recovery snapshot generation matches a committed Git ref; all LFS OIDs
  checksum-verified. No restoration from a mere Git bundle if LFS missing.
- Syncthing `cert.pem`, `key.pem`, device/folder config and peer identity
  present; client-side E2EE password remains solely client-side.
- Application account identities, entitlements, Stripe reconciliation status,
  audit and secrets available from a consistent recovery point.
- Placement node label may change on failover, but an existing PVC is **not
  relocatable across clouds**. Recreate from restored bytes, not by toggling
  Kubernetes node affinity.
- Real failover requires reliable cross-provider WireGuard/Tailscale/private
  transport, quorum and working failover credentials **outside** the failed
  provider. Test recovery for failure of each provider independently.

## Uncovered attack scenarios (prioritized)

| Scenario | State | Required coverage |
|---|---|---|
| Ransomware on a trusted client publishes encrypted/removed files | Partial: Git old commits survive normal push | immutable independent recovery copies; revoke compromised device; client UX restore |
| Stolen user's SaaS password/JWT creates malicious device | Open | MFA, device approval + per-device audit, session revocation, notification |
| Compromised Sync container uses its Git writer password | Partial: non-FF/deletes denied | protected independent history and S3; rate limiting and signed snapshots |
| Compromised History pod/Gitea admin/database/host | **Open** | cross-cloud immutable snapshot, isolated credentials, verified integrity |
| Compromised Control Plane or broad cluster RBAC steals tenants' Kubernetes Secrets | **Open** | split cluster/tenant permissions, external secret vault and scopes |
| Entire Hetzner region failure | **Open** | complete replica on OVH + cold/warm restore with fencing |
| Entire OVH region failure | **Open** | complete LFS and Gitea restore from Hetzner |
| Cross-cloud link partition/split-brain | **Open** | independent witness/quorum; strict single writer |
| S3 operator/delete-key compromise destroys historical LFS | **Open** | replicated encrypted S3 objects + immutable recovery, independent IAM |
| A user's 100 MB deleted file remains referenced in unrelated Git/LFS backup | **Open** | refcount + timed purge orchestration, truthful expiry |
| Old Object Lock expired on year-old object | **Open** | rolling protected recovery / protection at deletion event |
| Quota bypass by sending more data than plan | **Open** | hard per-tenant disk and LFS limits, monitored throttle |
| Denial of service / noisy neighbor exhausting CPU/disk/network | **Open** | load tests, per-plan IOPS and request quotas |
| Billing webhook forged/replayed | Partial: signatures + idempotency | out-of-order reconciliation, real Stripe test lifecycle |
| Compromised operator account, CI/CD image, supply chain | **Open** | signed/pinned images, multi-person approval and restricted deployment |
| Lost trusted encryption key / irrecoverable client | **Open** | client-side recovery phrase + second trusted device, never SaaS-escrow |
| Theft of user's encrypted data with weak E2EE password | **Open** | secure key derivation, rotation and independent security review |
| TLS/mTLS/certificate rotation or network spoofing across clouds | Partial: pinned internal TLS | secure service identities and automated rotation |
| Disaster affecting both cloud accounts simultaneously | **Open** | independent offline/offsite encrypted backups and tested drills |

### MVP exit criteria

Do not claim completed MVP or automatic HA until:
- forced loss of A and forced loss of B each pass automated full encrypted
  history + native Syncthing identity recovery tests;
- no third data-bearing container per tenant, no unexpected ingress or write
  endpoint, no cross-tenant escape, and NetworkPolicies enforced;
- 7-day delete/undo works with real ciphertext + Git LFS and releases actual
  disk space after the appropriate retention period;
- Stripe and recovery scripts are validated with production-equivalent
  external systems and costs support all three paid plans.
