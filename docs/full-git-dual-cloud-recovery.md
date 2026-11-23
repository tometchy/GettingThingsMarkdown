# Full Git in two independent clouds — engineering design, pricing, recovery

Date: 2026-11-23. This architecture is preferred **for new tenants** but is
not yet a production-tested two-provider HA system.

## Decisions

1. Exactly two resident per-tenant containers: **A (Syncthing + Git)** and
   **B (Gitea + SQLite + full local bare Git)**. B has no public write endpoint.
   Only Sync's scoped Git writer can append to the archive; the Control Plane
   uses a separate read-only Gitea account.
2. No S3 or Git LFS is needed for **new accounts**. Keep the legacy LFS mode
   as a nondefault migration path until every previously uploaded object has
   been validated and safely rewritten into plain Git history. Setting a
   flag does not magically convert old LFS pointers back to file contents.
3. Both sites hold the **complete encrypted Git commit tree**. Sync also
   holds the encrypted current files; Gitea holds Git objects in a native
   bare repository. New tenants provision nominally **3× live quota** on A
   and **2× on B** (2Gi each for small local dev), *not* a guaranteed bound:
   unlimited historical versions can exhaust either disk. Hard history quota,
   IOPS/CPU protection and real customer usage are required before launch.
4. Tenant Syncthing identity is age-encrypted into the SAME remote Git
   repository's `gtm-recovery` branch and remains in its own local Sync
   repository. `gtm-infra` contains the separate operator-encrypted
   bundle of `gtm-credentials`, `history-tls` and `history-ca`.
   No SaaS user plaintext or E2EE folder password is included.
5. Set `GTM_RECOVERY_AGE_RECIPIENT` to **two or more comma-separated
   age1 public keys** (ideally three: Hetzner, OVH and offline).
   Hold the corresponding **private identities** separately in the
   two independent cloud secret stores. **Kubernetes Secrets alone are NOT
   encrypted at rest unless Kubernetes encryption/KMS is enabled, and any
   cluster administrator with secrets read rights may extract the key.**
   Recommended: OpenBao with a third independent quorum voter/witness, or
   separately encrypted KMS-backed Kubernetes Secrets in BOTH providers.
   No private age key is mounted into a normal tenant pod; it is required on
   the trusted operator's recovery workstation for an explicit procedure.
   Recipient rotation automatically creates a new encrypted identity snapshot.
6. For **all user account settings** also back up **Control Plane SQLite**,
   Stripe event reconciliation state, JWT signing material, billing prices
   and configuration in both providers. This remains outstanding: storing
   tenant secrets in Git **does not** back up a separate billing database
   or make two cloud providers automatically available.

## Operator workflow (dev / tabletop, not production HA)

### Separate cloud-held age key installation

Generate distinct `age-keygen -o` identities **on trusted operator systems**.
After independent Kubernetes clusters, namespaces and KMS-backed encryption are
ready, use `scripts/recovery/install-cloud-age-keys.sh context-a key-a.txt
context-b key-b.txt` as an inspection-only dry run. `--apply` changes both
secret stores **only after** an operator has independently verified their KMS
at-rest encryption and sets `GTM_KMS_AT_REST_VERIFIED=yes`. This installer
checks that kube-system namespace UIDs differ, reducing risk of accidentally
storing both recovery keys in one cluster. Separate RBAC, private access to
the management Kubernetes APIs and provider-independent KMS are still needed.
This installer is a tool, not evidence a production secret manager is running.

### First setup

- Configure real Hetzner and OVH nodes labeled `gtm.io/cloud=hetzner` and
  `gtm.io/cloud=ovh`. The tenant controller's pod affinity is optional until
  the nodes are actually present.
- Use `scripts/recovery/export-tenant-secrets.sh gtm-<id>` on a **trusted
  operator host** with `kubectl`, `jq`, `age` and
  `GTM_RECOVERY_AGE_RECIPIENTS=age1...,age1...`. The file
  `tenant-secrets.json.age` is committed to the `gtm-infra` branch in
  the same Git repo; only ciphertext passes through Sync. Run again
  whenever any tenant Secret changes. Do NOT expose key material through
  the customer UI or log files.
- Ensure `gtm-recovery`, `gtm-infra` and `main` exist and are
  replicated, and verify each encrypted snapshot with a different
  cloud-held private age identity. Verify all Git blobs are in both
  complete Git stores, not LFS pointers.
- Give operators their own authenticated recovery service backed by
  separately controlled Kubernetes KMS / OpenBao instances. **Two-node Raft
  cannot preserve a majority after losing one site**; a third witness/
  voter or operator-controlled break-glass must be available. The
  reference setup does not provision this yet.

### Scenario A: Hetzner lost, OVH survives

1. Confirm the outage is a loss, not an inter-site network partition.
   **Fence the old Hetzner Sync instance** via an independent provider API,
   or do not promote. Preserve immutable incident logs.
2. On the surviving OVH Git run
   `scripts/recovery/inspect-tenant-archive.sh gtm-<id> history`.
   Verify main ref, gtm-recovery and gtm-infra, and complete Git fsck. A
   clean git fsck does not prove every encrypted file decrypts.
3. Recover `gtm-credentials` / TLS from `gtm-infra` on a trusted operator
   host using an OVH-held age private key:
   `scripts/recovery/restore-tenant-secrets.sh gtm-<id> history /path/key --apply`
   (requires `GTM_FENCE_ACKNOWLEDGED=yes`, and a reachable Kubernetes API).
4. Provision a **new empty** Sync PVC at OVH. Old Hetzner-bound PVC cannot
   simply attach to another cloud; reserve sufficient HDD space and select
   the OVH CSI/storage class explicitly. On a one-shot recovery environment
   mount the OVH private age identity (not into the normal running container),
   mount the original secret and CA, and run
   `/usr/local/bin/recover-from-git.sh`. It refuses non-empty PVs.
5. Restore Sync encrypted tree, Git object history, certificate, device key,
   all encrypted settings branches, and verify the **same Syncthing Device ID**.
   Only after integrity checks promote the replacement to active Sync.
6. A manual failover placement override exists
   (`recovery.failoverTargetCloud: ovh` with
   `recovery.fenceAcknowledged: true`), but the new PVC, network routes,
   control-plane reachability and node capacity **are not created by the
   override**. Two separate surviving workers are required by hard pod
   anti-affinity.

### Scenario B: OVH lost, Hetzner survives

1. Fence the old Gitea/History server and stop writing while recovering.
2. Verify the full A archive and encrypted branches:
   `scripts/recovery/inspect-tenant-archive.sh gtm-<id> sync`.
3. Recover tenant secrets from the independent Sync-side encrypted bundle:
   `scripts/recovery/restore-tenant-secrets.sh gtm-<id> sync /path/key --apply`
   with explicit fencing. Create **fresh** History PVC using a Hetzner-local
   storage class, restore Gitea TLS and credentials, and let the image
   initialize SQLite and its one private repository.
4. After Gitea is ready, the trusted operator can invoke the already
   installed `/usr/local/bin/reseed-history.sh` inside Sync with
   `GTM_FENCE_ACKNOWLEDGED=true`. It pushes the complete encrypted Git
   main and both protected configuration branches into newly initialized
   Gitea. Verify ref hashes and full Git fsck on both ends.
5. Manual placement override:
   `recovery.failoverTargetCloud: hetzner` and fencing acknowledgement.
   Switch back to separate clouds only after verified resynchronization.

### Important production limitations

- This is **not automatic failover**. The Kubernetes control plane currently
  runs once, its PVC and users/Stripe DB are not redundantly recoverable.
  Provider-specific PVCs, private networking, sealed secrets, real fencing
  and a third witness have not been deployed or tested with actual clouds.
- No guarantee RPO=0: the current Git commit loop pushes periodically. Loss
  of A between encrypted Syncthing arrival and confirmed Git push loses
  those unreplicated changes. Synchronous acknowledged replication would
  be required for a strict zero-data-loss promise.
- User master encryption/recovery password is never in either tenant
  server; if lost and no second trusted client holds it, the service
  cannot decrypt their old data.

## Seven-day deletion and ransomware

The desired UX: user intentionally removes an encrypted historical filename
with *fresh reauthentication* (ideally hardware passkey/MFA); keep both
repositories' old objects reachable for 7 days; user can Undo any time.
Space remains charged until the seven-day period ends. Admin can temporarily
grant 1 GiB (or other bounded amount) at no cost via `/admin/`.
After seven days, a specially authorized worker freezes Sync writes,
rewrites main history using `git filter-repo` on an isolated clone, verifies
the rewritten tree, and updates both sites atomically as far as possible.
Recovery branches must be **excluded** from arbitrary history rewrites.
Reflogs, keep-refs, mirrors, Git GC and backups must be carefully purged
after expiry. None of this delete pipeline is implemented yet; never tell
users their files have been irrevocably purged. A normal Git push refuses
force updates by design.

The password itself is not proof against malware that can keylog, hijack
sessions, control a trusted endpoint or corrupt signing material. More
importantly, an administrator with host-root on both providers can destroy
both Git copies. Requiring a human password does not prevent that. To
guarantee **no administrator can destroy a 7-day recovery copy**, it must
be enforced by independent immutable storage/hardware (WORM) or independently
controlled append-only service. Ordinary Git files, chmod, chattr immutable
and Gitea branch protections are not equivalent to that guarantee.

## Economics

Advertised quotas remain Mini 10GiB $1.99, Plus 75GiB $5.99, Pro 500GiB
$24.99, but actual provisioned blocks now span **5× nominal live quota**
(3× A + 2× B), not counting additional forensic/immutable backups, the
quorum/witness, shared databases, VAT and support. Full local Git can make
the proposed prices unsustainable under high customer utilization/history
churn, especially with OVH's SSD VPS-1 (only 40GB, unsuitable for dense
full-Git storage). Confirm dedicated **HDD hosts in BOTH independent clouds**
and tenant density from an actual cost model and benchmark before accepting
paid customers. The PV multipliers are reservation guesses, *not measured
history retention costs*. The exact-price model requires recalibration.

Relevant docs:
- https://git-scm.com/docs/git-gc
- https://git-scm.com/docs/git-reflog
- https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository
- https://openbao.org/docs/concepts/ha/
- https://developer.hashicorp.com/vault/docs/internals/integrated-storage
