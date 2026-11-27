# Recovery witness (reference implementation, not live fencing)
Independent trust domain, separate from A and B, only recovery metadata.
No tenant contents, no storage credentials, no third persistent data copy.
Run Python 3 + OpenSSL with a root-owned private 0700 state directory,
separate pinned Ed25519 keys for witness and each replica agent, config
managed by trusted provisioning. Config schema:

    {"tenants":{"gtm-<32 hex>":{"sync":{"cloud":"a","publicKey":"/keys/a.pub","sourceUrl":"https://a.internal/repo.git"},"history":{"cloud":"b","publicKey":"/keys/b.pub","sourceUrl":"https://b.internal/repo.git"}}}}

Do not place the witness key or the independent replica evidence keys in
tenant workloads. Commands in server/integrity/recovery_witness.py:
explicitly initialize each new tenant offline exactly once; challenge anchor; collect replica_evidence.py attest on stable snapshots in
both clouds; anchor with both signed files. A challenge is single-use,
time-bounded and bound to the previous generation/ref map. Both agents
verify full Git and append-only ancestry. Non-head refs cannot be rewritten
or removed. No purge support. The witness serializes each tenant transition
with flock, atomic file replace and fsync of file and directory, and signs both the
monotonic generation record and complete witness metadata state. Reads verify
signatures, generation/ref digest and configured trust fingerprints; trust
changes require an explicit future protocol, not silent key replacement. Ref maps are metadata only. No implicit
reset on missing or invalid state. Reinitialization is an offline privileged
provisioning action and still requires an independent watermark in production. Restore from an old witness backup is not safe:
independent anti-rollback storage/watermark and HA remain required.
 
For repair: challenge repair --target-role sync --target-pvc <uid>;
collect signed history evidence matching the anchor; start-repair; ticket
<operation> <new-output-directory>. The ticket matches authorized_rebuild.py
and pins all refs and target identity. Staging must occur on a verified
new volume, never an existing live repo. The target trusted agent signs
the receipt after independently verifying its provenance. The witness
requires physicalScrub=true and matching ticket digest/identity. It records
staged_verified, never activation. Repeated issue always returns the same ticket, even after expiry. Expiry
is not proof that a running transfer stopped. A new attempt or abort after
issuance requires fresh signed target evidence that the expired attempt is
stopped and isolated (retire-attempt). A completed attempt can still submit
its signed receipt; staged_verified never authorizes activation. The trusted
target must actually establish stop/isolation before signing; no automatic
provider fence is inferred. The CLI and optional private mTLS API are for trusted orchestration; actual
agent authentication channel, external monotonic watermark,
automatic ticket worker or storage/job binding yet. A ticket expiry governs
start, not guaranteed termination of an in-flight rebuild. Do not activate
or reuse a staging target while a job may still be running.
 
Challenges permit up to 48 hours for a complete scan on a stable snapshot;
submitted evidence must have completed within the last 24 hours. These are
initial budgets to benchmark under real load, not a measured 24/48h SLO. The witness deliberately
freezes generation advancement during active repair (not actual writers).
Actual writer enforcement, quorum during partitions, independent retention,
provider fencing, stable snapshot, correct full-Git branch consolidation,
and verified cutover are still mandatory. This CLI does not authorize writes
or make the service production ready.

## Private network API
Build server/recovery-witness/Dockerfile and deploy only in the independent
trust domain, not alongside tenant data. Mount the private persistent state
(0700 UID 1000), config/public keys, witness Ed25519 private key and separate
TLS certificate/key/client CA. The API defaults to loopback, bind to a private
interface only after firewall approval. TLS 1.3 and mutual certificate
authentication plus exact SHA256 DER coordinator certificate allowlist are
required. No public ingress or general admin API. POST /v1/recovery supports
status, challenge, anchor, start-repair, ticket, receipt, retire-attempt, abort; never
initialize or activate. Use witness_client.py with a pinned server CA and
coordinator cert/key. API cert holder still cannot forge either replica's
independent evidence. CI tests real TLS with approved and unapproved certs.
No remote provisioning or automatic HA deployment is performed.

## Trusted staging runner
stage_runner.py connects via mTLS, obtains the current witness repair/ticket,
checks independently supplied tenant/generation/PVC UID/role/cloud, uses a
verified separately provisioned mount (real mountpoint + expected device
major:minor + restrictive owner permissions), and locks the staging root.
It rebuilds into a unique attempt directory, runs physical scrub, signs and
submits a receipt. If submission fails after completion, a repeat on the
same attempt re-verifies all staged data before signing again, even after
expiry. An expired ticket cannot begin a new fetch.
Incomplete staging is never overwritten; retire the old attempt with
verified target evidence before separately authorizing another attempt.
The allocator must establish the real PVC→mount/device identity outside this
worker; these checks alone cannot prove Kubernetes binding. The target signing
key and mTLS credentials belong to a trusted host/repair service, never a
normal tenant pod. No Kubernetes write permission, no activation. Do not
reuse the volume until any old job is stopped/fenced. Real Git and OpenSSL
CI fixture simulates interrupted receipt delivery and verifies restart.

## Consensus metadata backend (optional)

For distributed operation use an independently provisioned three-member
etcd cluster, one voting member in A, B and the independent witness failure
domain. Store only signed recovery metadata under /gtmd/recovery/v1/;
no Git objects, data files, keys or third data copy. Configure mutual TLS
for peers and clients, encryption/permissions and a prefix-scoped read/write
identity for witness API; deny all other access. Use --etcd-config instead
of --state-dir with JSON:

    {"endpoints":["https://etcd-a.internal:2379","https://etcd-b.internal:2379","https://etcd-w.internal:2379"],"ca":"/tls/ca.crt","cert":"/tls/client.crt","key":"/tls/client.key"}

The v3 gateway client uses linearizable range and transaction compare of
mod_revision; concurrent witness transitions cannot overwrite newer state.
A failed/ambiguous transaction is not replayed blindly. Read current state
before retrying. No quorum means no new decisions. Max signed value 512 KiB;
large ref sets fail closed pending measured metadata design. Local backend
is for testing/one authority only. CI launches three real etcd members,
races CAS and witness challenges, stops two members, verifies minority
cannot read/write, restores quorum and compares state. HTTP is accepted
only through explicit loopback test mode, not CLI production config.
Real multi-cloud mTLS/IaC, peer latency and failure-domain validation,
external anti-rollback watermark after full cluster restore, writer
enforcement/fencing and independent retention are not yet complete.

## Writer freeze and short-lived permits (opt-in)
Offline enroll-writers binds both roles to exact pod, all PVC and node UIDs. The
trusted host agent signs renewal requests with its role key and publishes
witness-signed 60s permits in a host-owned directory, mounted read-only at
/run/gtmd-writer-fence with pinned witness.pub. Renew every 10–15s, not via
slow Kubernetes Secret refresh. The data image uses GTM_WRITER_FENCE_MODE=required,
GTM_FENCE_TENANT, GTM_FENCE_POD_UID, GTM_FENCE_PVC_UIDS (comma-separated all data PVC UIDs), GTM_FENCE_NODE_UID;
the guard must be PID1 in an unshared container PID namespace. Default
development mode preserves legacy behavior and is NOT production fenced.
No deployment automatically sets these variables yet. Provisioner must
verify exact mount and identity and forbid override/bypass/exec and shared PID.
Freeze-writers advances epoch atomically and denies renewals. Both roles must
provide signed stop/isolation evidence after all prior permits expire plus
clock-skew allowance. A stopped pod status, elapsed lease or deleted API
object is NOT by itself proof of actual isolation. Trusted host or provider
adapter must establish runtime stop, revoked write access and isolated volume.
Only then can an enrolled tenant proceed to a repair challenge. The source
must be served by a separately authenticated read-only snapshot endpoint;
normal Gitea/Syncthing writers are stopped. That endpoint and collection of
actual provider/host evidence are not implemented yet. No release/unfreeze
or replacement writer activation. cutover-status lists mandatory blockers.

## Read-only recovery source
Trusted host service recovery_source.py serves a verified stable snapshot
through HTTPS Git upload-pack only (/repo.git). Require a read-only mount,
exact refs manifest, signed generation anchor from witness, pinned witness
public key and independent target client TLS certificate fingerprint.
No receive-pack or web UI, no Git private data beyond the authorized repo.
Configure sourceUrl in witness trust provisioning to this endpoint, not an
unavailable stopped Gitea. TLS client cert/key/CA passed to the staging Git
process through GIT_SSL_CERT/GIT_SSL_KEY/GIT_SSL_CAINFO from a trusted host.
Service must be isolated and launched only after verified source selection;
source must be the existing replica or a transient filesystem snapshot of
it, not a third persistent data copy. CLI verifies read-only mount and exact
refs; it does not mount disks or prove physical isolation. Tests use a
stable disposable repo and real mTLS Git fetch, not a real provider mount.

## Restartable orchestration
recovery_orchestrator.py reconciles one immutable incident in the witness.
plan-repair atomically freezes enrolled writers and pins target role, NEW
PVC UID, anchor and generation. The orchestrator accepts fresh trusted stop
proofs, waits for both, issues or resumes a signed source challenge, consumes
source evidence and creates/reuses a durable staging operation. The separate
stage runner fetches the signed ticket and submits its receipt. Later calls
report staged_verified with cutover blockers. Pass challenge-output to write
private challenge metadata; ordinary status logs omit refs. No automatic
allocator/host fencing evidence collection, staging Job scheduling, live PVC
changes, unfreeze or provider API operations. The controller is not yet wired
to this orchestrator; run only in trusted test/orchestration environment.

The read-only recovery_binding.py inspector derives writer identities from
all PVC references (including sync-live where present), exact bound claim/PV
UIDs, node inventory and separate clouds. It validates an already allocated
new bound Retain PVC on a different healthy node in the same cloud, with
compatible storage class/mode/capacity, no references by tenant pods.
This is API-level validation only, not actual mount or physical isolation.
Needs read-only scoped pod/PVC API access; does not allocate or mutate.
