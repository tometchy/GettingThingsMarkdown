# MVP acceptance and blockers

Status: Engineering preview. The account/Stripe flow, browser/desktop Avalonia
account screens, two-tenant kind provisioning, non-S3 E2EE -> encrypted Git ->
HTTPS Gitea -> native offline decrypt have passed selected GitHub Actions jobs.
**Production multi-cloud operation, hard history quotas, live storage migration, paid Stripe flows and Android/iOS are not yet proven.**

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
- [ ] **Shared account/payment state**: Control Plane SQLite, Stripe event
  reconciliation, JWT secrets and entitlement decisions independently backed
  up on both providers. Per-tenant identity/config encrypted Git backups exist.
- [ ] **Legacy LFS tenants only:** migrate all historical objects to native
  full Git with verified checksums; LFS pointers do not store full bytes.
- [ ] 7-day password/MFA-protected history erasure request/Undo, rewrite and
  post-expiry native Git garbage collection on BOTH replicas. Current 30-day WORM backup conflicts
  with this policy: do not enable it for production.

## History protection and non-privileged operations (2026-11-23)

- [x] Durable per-account history deletion intents using opaque encrypted paths, fresh account password verification, replay/idempotence controls, seven-day minimum deadline and Undo while pending.
- [x] Admin can only **extend**, not shorten, a whole-tenant retention hold for reported ransomware incidents; reason and change are audited. No admin route can read the tenant's Git objects or access its pods.
- [x] Read-only admin observability summary (aggregate tenant/provisioning health, pending intents, holds and explicit disabled safety flags) plus database-backed readiness probe.
- [ ] No automatic purge after the seven-day period. An intent records a requested operation; it neither rewrites Git nor frees disk. Requires independently enforced immutable retention, separately privileged double-replica rewrite, verified GC, concurrency control and reproducible chaos tests.
- [ ] Incident-hold/intent state plus account/Stripe state must survive losing either provider without rollback; the single SQLite control-plane PVC is not redundant.
- [x] Production administrator API is disabled by default, even if an administrator token is configured; local Development remains authenticated.
- [ ] Operator SSO/MFA, non-public admin ingress, separate read-only admin identity, independent security audit ledger and verified root-resistant retention in both physical sites.
- [ ] Full automatic A↔B failover with real fencing/quorum, new local volumes, restored secrets and routing; not safe to switch on automatically in this branch.

See [history-protection-operations.md](history-protection-operations.md).

## New full-Git direction

- [x] Manual, explicitly fenced placement override may schedule both tenant
  roles on surviving provider; still requires two separate surviving nodes.
  Does **not** make a failed provider PVC magically attachable elsewhere.
- [x] Operator-only encrypted secrets export to the `gtm-infra` branch
  in the same Git repo, under >=2 independent age public recipients.
  Each cloud must independently hold the matching private age key securely.
- [x] Cold full-Git Sync restore script uses operator-supplied private age key,
  restores same Syncthing certificate and encrypted Git working directory;
  offline integration exercises A->B and B->A.


- [x] New tenants default to **full local Git** without S3/LFS; when production-sized,
  Sync PVC requests 3× plan GiB and Gitea/History PVC requests 2× to reserve
  space for active encrypted data and independently complete Git history.
  These are reserve assumptions; Git history must have an enforced cap.
- [x] Admin-gift model: audited, optional free 1..1024 GiB bonus expiring in
  1..30 days, visible in shared Avalonia account view and editable from
  administrator page; existing paid plan and Stripe price are unchanged.
- [ ] Test no-LFS recovery of both pods from one surviving provider, with
  complete Git refs and encrypted service secrets.
- [ ] Existing LFS tenants need validated `git lfs migrate export` and
  cross-provider verification before turning off legacy LFS; never silently
  point new plain Git at LFS pointer-only history.
- [ ] Physical quotas must include history and reserve space; currently
  gift changes the declared PVC size, but does not enforce a logical cap.

## Plus / Pro SMTP → tasks (engineering preview)

- [x] Random 192-bit forwarding recipient aliases, Plus/Pro eligibility checks and independently authenticated encrypted mail queue (no plaintext mail disk writes).
- [x] Hybrid RSA-3072/OAEP-SHA256 and AES-256-GCM ciphertext envelopes; trusted desktop keeps private key locally and produces native `ToDo/*.md` files for standard Syncthing E2EE.
- [x] Alias registration/rotation, HTML forwarding verification URL handling, mailbox/queue endpoints, bounded message size/dedup and automatic import polling while trusted desktop is running.
- [x] Optional shared SMTP service/deployment (not one SMTP container per tenant), encrypted queue ingress, localhost kind smoke script.
- [x] CI passed standalone SMTP/STARTTLS → encrypted inbox → ACK using disposable kind (run #45232220656). External Gmail verification and public DNS/MX/TLS are still untested.
- [ ] Durable multi-cloud inbox/account persistence, independent recovery keys, strong spam/flood/throttle policy, production auth/network hardening and tested mobile import/background behavior.
- [x] Non-destructive ACK and 30-day encrypted mail recovery archive, with incident-hold extension, idempotent ACK and a trusted-client restore action. Retained ciphertext is capped and the cleanup worker never removes unacknowledged mail.
- [x] Serialize manual and automatic imports on the same desktop engine.
- [ ] Confirm mailbox sync/commit has reached **both** encrypted Git replicas before relying only on normal mail/archive retention; idempotent filenames alone cannot provide a distributed exactly-once guarantee.

See [SMTP feature and security contract](smtp-encrypted-inbox.md).

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

## Git-only inbound mail and access isolation (PR #9)

- [x] Implementation: full original MIME retained in public-key envelope, attachments extracted on trusted client, independent RSA device recipients, Git-only mailbox branch in the two existing archives, no time-based expiry.
- [x] Implementation: client reads paginated Git history and imports locally; Syncthing only synchronizes normal task, attachment and receipt files. No extra Syncthing peer.
- [x] Implementation: private-IP Scaleway workers, outbound NAT, deny-public API and scoped operator IAM configuration. Real provider access verification remains necessary.
- [ ] Production independent cloud recovery, account/mail registry recovery, mobile clients, history purge and quotas.

See [Git-only mail design](gtmd-mail-files.md). Previous 30-day archive and additional mailbox peer descriptions are superseded.

## Storage and recovery safeguards (PR #10)

- [x] Fix legacy LFS smoke test to wait for the actual encrypted file, not an empty initialization commit.
- [x] Measure Sync encrypted live bytes and allocated Git/recovery bytes, expose a tenant-scoped read-only usage API and display in desktop pairing UI. Conservative history budget 1.8x effective live allowance and disk reserve; new SMTP data rejected before Git commit if projected usage exceeds budget.
- [x] Reconcile paid/gift quota and pause Sync at capacity or failed metering. No deletion to enforce quota. This is sampled admission, NOT a hard storage-driver limit; already-in-flight Syncthing writes and the replica B physical disk remain independent limits. A paused tenant may need a temporary free allowance to synchronize deletions.
- [x] Include the Git mail ref in A-to-B and B-to-A cold recovery and fail closed if remote ref existence cannot be verified.
- [x] Implementation: optional shared backup container with SQLite online backup, age encryption for two recipients, independently configured S3-compatible destinations, checksum verification after download and stale-backup readiness gate. Restore into a fresh staged directory only. Each DB snapshot is internally consistent, not an atomic cross-database transaction; live failover and Stripe reconciliation still required.
- [x] Isolated Git purge candidate builder: rewrite a literal opaque path, preserve unrelated refs, refuse a still-live main file, GC unreachable blobs inside candidate only, report blobs still referenced elsewhere. No publication or live deletion.
- [ ] Provider-enforced hard quotas, immutable seven-day retention, serialized hold/purge authorization and dual-replica cutover/retired-storage destruction, independently fenced failover, Android/iOS and real production acceptance remain mandatory.

See [storage and recovery operations](storage-recovery-operations.md).

## Selective quota and mail ledger (PR #11)

- [x] Remove quota-triggered whole-folder pause for eligible accounts; existing quota-paused accounts are resumed during reconciliation. Subscription expiry behavior unchanged.
- [x] Fresh full-Git tenants can use separate bounded live PVC without additional pods or increased nominal total reservation; existing PVC layout retained; no automatic migration.
- [x] New Kubernetes SMTP deliveries no longer write SQLite mail receipt rows; deterministic Git digest and serialized commit history enforce retry/rate policy.
- [ ] Verify real CSI physical capacity and native Syncthing deletion/download at full live volume, history-budget admission and safe downgrade/gift expiry. Shared PVC legacy migration, actual failover/purge and mobile apps remain open.

## Kontynuacja 2026-11-26 — logiczna historia i ekonomia

Nadrzędne wymagania: MVP-SPECIFICATION.md; wcześniejsze pozycje o migracjach, LFS/S3, centralnym SQLite i mnożnikach PVC opisują przeszły prototyp, nie kryteria nowego MVP.
- Stan main 80f38b152278ee8bebf582b85a7fe6c1c0d25dfb: CI 45817121204 — dotnet, browser, safety, infrastructure success; images failure w SMTP kind (HTTP 503). Poprzedni main 45797835070 success. Drugi curl --fail-with-body wskazuje na rejestrację mailboxa; przyczyna 503 niepotwierdzona. Wykryta osobna luka gotowości: Gitea mogła mieć repo bez main. Ready wymaga teraz odpowiedzi read-only dla branch main; dodano nazwy etapów smoke bez wypisywania sekretów. Nie osłabiono testu.
- Implementacja: niezależny pomiar unikalnych blobów main + refs/gtmd/protected/*, bez liczenia innych gałęzi lub fizycznej wielkości packfile; CLI dostępne wewnątrz obrazu Sync (nie na ścieżce SMTP). Brak automatycznego wyłączania technicznych ścieżek; jawnie ciphertext bytes. Nie włączono do publicznego limitu ani admission, nie istnieje jeszcze protokół chronionych refów, inkrementalny indeks, fencing lub dowód zgodności A/B. Testy Git: dedup, usunięcie, inna gałąź, GC, protected ref i brak obiektu.
- Implementacja: model kosztów czterech planów i pełnego wykorzystania, jawne założenia, scenariusz opłat mobilnych, nieustalona roczna cena max blokuje roczny gate. Nie uruchomiono zasobów płatnych. Wynik nowego CI do odczytania z PR; nie deklarujemy zaliczenia przed wynikiem.
- Nadal BLOKER: native selective admission przed materializacją i ACK, niezależna 7-dniowa retencja, sejf i obowiązkowy recovery kit, quorum/fencing i automatyczny failover; żadna z tych funkcji nie została uznana za gotową. Live purge pozostaje wyłączony. Nie osłabiać wymagań ani deklarować RPO=0.

## Git-native integrity and self-healing (mandatory, 2026-11-26)

Requirements: MVP-SPECIFICATION.md §17.1. Plan only; not implemented or tested by this change.
- [ ] Every transfer validates new Git objects and required committed refs; independently anchored manifest/fencing prevents silent rollback and stale-replica resurrection.
- [ ] Scheduled bounded full-content scrubs of A and B, all required/protected refs, and encrypted working tree; hourly ref/lag checks, continuous rolling full-content scrub (target 24h, critical 48h), missed-deadline and corruption alerts in both clouds.
- [ ] Automatic quarantining/fencing, verified source selection, targeted repair of missing/corrupt objects and indexes with forced transfer, full replica rebuild on new storage when required, verified atomic publication and safe resume; healthy replica stays protected.
- [ ] Fail closed on ambiguous source or unavailable quorum; audit, retry and incident visibility without manual SSH or leaking tenant content.
- [ ] Fault injection covers bit rot, missing objects, corrupt packs/indexes, rollback, legitimate concurrent changes, interrupted repair, total disk failure, retention/purge race, both copies corrupt and network partition. Test successful E2EE decrypt after repair.
- [ ] Benchmark HDD/JBOD with independent per-disk failure domains vs RAID6/mirror, fully used plans, repair and scrub cost, measured RPO/RTO, spare capacity, provider replacement SLA and independent retention. Local RAID is optional if all mandatory acceptance criteria pass; no third persistent copy or relaxation of two independent replicas.

### Continuous scrub and hardware lifecycle (§17.2)
- [ ] All branches, tags, refs, reflogs, loose/packed and not-yet-GCed objects checked in both replicas; actual disk reads, per-segment age and watchdog. Target 24h, critical at 48h; no silent extension. Test saturated pools, concurrent commits/repack, restart and repair, distinguish legally unreachable/purged objects.
- [ ] Node/disk health agent and stable disk identity; simulated SMART degradation, silent corruption, I/O errors, readonly filesystem, total disk/node failure; quarantine, alert and begin verified restore without awaiting vendor intervention.
- [ ] GTMD storage controller provisions new isolated local PV/PVC, fences old writer, restores and validates staging, preserves identity and retention, then switches workload/routing; no blind PV deletion or empty Syncthing startup. Test both directions and stale-node return.
- [ ] Provider-specific hardware ticket automation where supported (otherwise actionable alert), documented replacement SLA and workflow, no tenant content in ticket, validate replacement disk before reuse. Verify spare capacity for largest local disk failure, node/cloud disaster capacity, actual throughput, full recovery duration and costs. No paid deployment before approval.

### Implementation increment — Git integrity worker / isolated rebuild (2026-11-26)
- Implemented in server/integrity/git_integrity.py, installed in both existing images (no additional tenant container). Local full fsck/strict including unreachable objects and reflogs, verify-pack, missing pack/index pair detection, ref/pack stability checks, rejection of incomplete/alternate stores; low CPU priority rolling watch, five-minute gap between scans, 24-hour scan timeout. Independent local watchdog (60s sampling, 24h warning/48h critical, missing/stalled status detection), atomic per-replica status files (/data/integrity.json, /var/lib/gitea/integrity.json) and redacted JSON log events. Git transfer verification enabled on A/B. Scan failure does not mutate data or authorize any destructive action.
- Implemented stage-rebuild: fresh isolated bare Git, fetch all refs, no hardlinks/alternates, expected exact ref manifest and full verification. Caller must obtain authenticated manifest independently; no trusted manifest issuance/verification, no live publication. Reflog-only/unreachable protected content must be explicitly rooted before cutover; current legacy separate recovery repositories not covered by A main scan. Existing prototype LFS bytes not validated by this worker. All required data must ultimately be in one full-Git repository per replica.
- CI tests added: loose/old/unreachable blob corruption, non-main missing blob, corrupt pack, missing index, stale-but-valid ref set, alternate refusal, isolated rebuild and status redaction. First PR CI safety job 140161998237 failed because read-only Git objects could not be mutated by the unprivileged fault-injection fixture (three PermissionError cases); corrected fixture to chmod only disposable test files and added independent corrupt-index/watchdog tests. New CI result not asserted until run completes.
- [ ] Physical uncached scrub, per-segment age, actual 24/48h SLO, filesystem/SMART and node agent, cluster-external watchdog for stopped/missing pod, scrape/alert routing and cross-cloud manifest/lag. Local full scan can race writers/repack and retry; no global writer lease or stable snapshot yet. Local Git validation alone cannot recognize corruption that was already committed as valid new data.
- [ ] Secure source selection, automatic quarantine, targeted repair, working-tree byte verification, protected refs and independent retention, fenced Kubernetes volume repair and cutover, provider tickets, all-branch full-Git tenant state, chaos/load tests. No live automatic repair or data-loss protection claim. No paid infrastructure.

### Hardware health implementation increment (2026-11-27)
- Added opt-in root host systemd probe (server/integrity/disk_health.py, infrastructure/storage-health) for inventoried physical ATA/NVMe disks with stable by-id and serial verification; bounded SMART collection, missing/unsupported/identity mismatch = unknown, media degradation and current failure detection, optional Linux SCSI I/O error counter. Structured redacted events and atomic Prometheus textfile metrics, private mode-0600 hardware mapping; independent scrape freshness/health rules. No extra tenant container or Kubernetes credentials, no disk write/eviction. Fixtures for degradation, failure, absent disk, malformed telemetry, replacement identity and invalid inventory wired to CI. Hardware deployment is not yet enabled and requires provider-specific host provisioning and real hardware verification.
- Host service intentionally not deployed as a privileged tenant DaemonSet; SMART requires host privileges. Still open: real node_exporter wiring and expected-node disappearance, kernel logs, mapping disks→PV, automated quarantine and rebuild, physical uncached Git scrub, provider tickets, independent retention and quorum. No automatic repair claim. See infrastructure/storage-health/README.md.

- Added optional temperature telemetry and increasing interface CRC alert, explicit failing SMART status even when other commands fail, and executable promtool rule fixtures for missing/stale/unknown/degraded/failed hardware. No claim of a running production monitoring stack.

- Added minimal private local Git health metrics endpoint in both existing containers and tenant NetworkPolicy ingress restricted to the dedicated monitoring namespace/pod label; scrape configuration example and alert rules, no public Service or content. No actual monitoring cluster deployment; expected-tenant target disappearance and real CNI enforcement need tests, as does physical scrub.
