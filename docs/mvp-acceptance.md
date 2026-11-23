# MVP acceptance and blockers

Status: Engineering preview. The account/Stripe flow, browser/desktop Avalonia
account screens, two-tenant kind provisioning, non-S3 E2EE -> encrypted Git ->
HTTPS Gitea -> native offline decrypt have passed selected GitHub Actions jobs.
**Production operation, paid S3 restore and Android/iOS are not yet proven.**

## Newly hardened, but still not production redundancy

- [x] Gitea reader is a separate, unprivileged collaborator. Only Sync holds
  its Git-writer password; account-facing history requests no longer use the
  repository owner/admin identity. Added per-role ingress NetworkPolicies.
- [x] Optional provider-specific nodeAffinity: Sync on a `hetzner`-labeled
  worker, History on an `ovh`-labeled worker, validated for distinct values.
- [ ] Kubernetes NetworkPolicy actually enforced by a suitable production CNI;
  kind default networking alone is NOT proof.
- [ ] Real, fenced automatic failover; cross-cloud network, independent storage
  recovery, active quorum/witness and separate cloud bootstrap credentials.
- [x] On Sync start, a background recovery job backfills historical LFS objects
  from remote Gitea/S3 (including tenants whose old versions were pruned);
  CI exercises delete-local-object + rehydrate. No full quota guarantee.
- [x] Sync retains encrypted Git LFS source objects after push; allocated
  Sync PVC nominally 2× plan quota in LFS mode. This still needs actual history
  byte quota, capacity enforcement and fault-injection recovery.
- [x] Optional, age-encrypted Syncthing identity/config snapshot pushed to
  Gitea on a separate Git branch, retained in local Sync Git too. CI must
  prove a cold decrypt + exact key/cert recovery. Offline key is operator-held.
- [ ] Redundant signed/encrypted user settings + stable Syncthing identities
  stored on both clouds, not merely the control-plane SQLite PVC.
- [ ] Both provider copies contain all needed historical LFS bytes. Currently
  two Git repositories do NOT imply two copies of offloaded Git LFS.
- [ ] 7-day client-originated deletion rewrite/undo, quota accounting and
  post-expiry LFS garbage collection. Current 30-day WORM backup conflicts
  with this policy: do not enable it for production.

## Release acceptance checks

- [x] Backend with 1-month trial, admin extension, Stripe test-mode Checkout
  and signed webhooks implemented. Six recurring prices validated against the
  public three-plan catalog (amount, USD, interval), no amount from client.
- [x] Linux Mint development-only 100-year trial gated by Development flag.
- [x] Shared Avalonia desktop/WebAssembly PL/EN subscription UI; no duplicated
  markup for subscription management.
- [x] Kubernetes tenant namespaces with two long-running tenant workloads on
  separate worker nodes.
- [x] End-to-end **without S3**: E2EE transfer, Git commit and push, native
  decrypt of encrypted Git historical snapshot.
- [x] Independent affordable-storage code path: Gitea Git LFS backed by S3,
  per-tenant prefix, small Gitea metadata volume; pricing stress model.
- [x] Old 15/200/1024 GiB accounts retain their quotas on migration.
- [ ] Verify full Gitea Git LFS restore and Syncthing plaintext recovery after
  retrieving all historical objects from S3 in an isolated test.
- [ ] Verify 30-day Object Lock COMPLIANCE retention and real restoration
  using provider-specific credentials. Test code presently uses stubs.
- [ ] Real disk hard quotas enforced by storage driver and tested at capacity;
  plan upgrade PVC expansion requires allowVolumeExpansion.
- [ ] Production node choice, independent failure domains, exposed Syncthing
  relay/discovery, TLS ingress, monitoring, backpressure, security review.
- [ ] Control-plane database, Syncthing identity, secrets and encrypted S3
  objects all backed up and restored in a documented outage drill.
- [x] Production email verification is required before allocating tenant pods;
  tokens are hashed, expire after 24h and are rate-limited. Paid checkout is blocked until verification. Existing users are
  migrated as verified; Development auto-verifies locally.
- [ ] Provider SMTP deliverability and abuse controls, Stripe sandbox invoices,
  failed payments, cancellations, refunds and tax settings; no live charges.
- [ ] Android embedded Syncthing native engine and iOS app functionality,
  signing and mobile background lifecycle tests.
- [ ] Local desktop GUI whole workflow validated on Linux Mint, including
  successful packaging, native process start, restoration and shutdown.
- [ ] Load test the two-container-per-tenant density and validate the
  $1.99 / $5.99 / $24.99 price structure against actual invoices.

Do not mark the project production-ready or promise data-loss protection until
all relevant mandatory checks have passed.
