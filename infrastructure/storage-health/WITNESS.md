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
normal Gitea/Syncthing writers are stopped. The endpoint is implemented below;
production snapshot provisioning and durable isolation evidence remain open. No release/unfreeze
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


## Host runtime observation and scoped stop

The independent witness now exposes resume-freeze through the existing
pinned mTLS API; the immutable signed challenge can be fetched after an
orchestrator restart. Use --freeze-output with recovery_orchestrator.py
to export it privately (not the separate repair scan challenge).

Install server/integrity/host_runtime_fence.py and its existing Python
dependencies in a root-controlled directory on the actual Linux node.
Use crictl at /usr/bin/crictl with the local runtime Unix socket. The
root-owned inventory file (not writable by group or others) must be
provisioned independently of tenant pods. Example schema (actual UUIDs,
not names; replace placeholders with independently verified values):

    {"tenant":"gtm-<32 hex>","role":"sync",
     "identity":{"podUid":"<UUID>","nodeUid":"<UUID>","pvcUids":["<UUID>"]},
     "devices":{"<PVC UUID>":"8:1"}}

Map ALL data PVCs to actual filesystem device major:minor on this boot,
including sync-live. The collector does not derive physical identity or
verify this mapping against Kubernetes/provider inventory; use it only
after the trusted host provisioner independently verifies the binding.
Shared filesystems conservatively report every potentially writable mount
of the mapped filesystem, including unrelated tenants. Device numbers
are not persistent identifiers. No support for virtual/anonymous
filesystems whose device major is zero.

    python3 host_runtime_fence.py --inventory /etc/gtmd/writer.json \
      --freeze-challenge /run/gtmd/freeze.json \
      --witness-public /etc/gtmd/witness.pub \
      --cri-socket /run/containerd/containerd.sock

Default is read-only. --stop-enrolled is an explicit development-only
operational action: after the signed challenge's not-before deadline it
calls CRI StopPodSandbox on exact enrolled sandbox IDs only, never on a
replacement UID. It does not delete the Pod/PVC or stop unrelated
tenants. Run only after checking that the signed freeze is the current
incident and the node identity is correct. The collector reads the local
CRI twice around the kernel sample, counts non-exited containers,
matching pod cgroups and all visible mount namespaces, and flags
potentially writable mounts (including ro bind mounts on rw superblocks).
Unknown runtime state, orphaned containers, malformed inventory, failed
CRI requests and inaccessible process observations fail closed.
It emits aggregate counts, no raw mount paths, file names or container IDs.

The resulting writer_runtime_observation is deliberately NOT a
writer_stopped attestation: stopEvidenceEligible=false and
activationAllowed=false even when every observation is clean. No private
key is accepted. CRI stop does not prevent kubelet recreation; proc
sampling cannot prove absence of detached mounts, raw-device writers,
unreachable hosts or future writes. Independent durable restart
prevention, actual physical binding and storage isolation, and trusted
provider fencing are still needed before the role agent may sign stop
evidence. Do not translate this report into the existing stop proof.
Tests cover signatures, identity, runtime races, actual /proc mountinfo,
and a real CRI sandbox stop in disposable kind. CI results must be checked
before declaring the integration verified.

Writer guard now uses Linux CLOCK_BOOTTIME for lease expiry across host
suspend; a resumed process may still run before the next supervisor
poll. This is not a replacement for external host/storage fencing.


## Kubernetes admission barrier (development, no release path)

Install the optional Helm writerFence.enabled=true policy and its Deny
bindings on Kubernetes >=1.30 before granting any actor RBAC. Provision
one independent service account per tenant in the configured chart system
namespace, named <tenant>-fence, using namespace_fence.actor_manifest().
It grants only GET/PATCH of that exact Namespace resource name; no pod,
PVC, PV, policy, secret or RBAC writes. Use short-lived TokenRequest
credentials outside tenant pods. Do not give tenant users token creation
or service-account impersonation permissions in the system namespace.
The actor can cause denial of service to its tenant if compromised, but
cannot remove the marker or activate a writer. CEL does not verify the
witness signature; the trusted actor does. A cluster administrator who
can remove admission policies remains outside this guarantee.

Install server/storage-controller/namespace_fence.py and kube_client.py
with the existing server/integrity Python modules on a trusted coordinator
(PYTHONPATH must include both directories). The inventory is independently
provisioned and pinned to the real Namespace UID and local role's enrolled
pod/node/PVC identities:

    {"tenant":"gtm-<32 hex>","namespaceUid":"<UUID>","role":"sync",
     "identity":{"podUid":"<UUID>","nodeUid":"<UUID>","pvcUids":["<UUID>"]}}

The adapter fetches the CURRENT signed freeze from the mTLS witness,
validates the signature and identity, then compares Namespace UID,
resourceVersion, and phase. Read-only by default. With explicit
--apply-development-freeze it dry-runs a malformed marker write and
requires the exact expected admission denial (generic 403/422 is
insufficient), then installs gtmd.io/freeze-digest with JSON Patch CAS.
Reconciliation is idempotent; different marker, missing policy, missing
namespace, CAS conflict or unavailable witness block the operation.
On both clouds apply against each independently provisioned namespace
and role. This is an explicit development operation, not yet automatically
called by the incident controller.

The marker is immutable, including for the actor; namespace delete and
finalize are denied. The dedicated actor cannot modify unrelated
annotations, labels, status or namespace spec. Frozen tenant namespaces
deny new pods (any name/label), pod updates including ephemeral containers,
binding, resize, new/changed/deleted PVCs, and pod exec/attach/portforward
connections. Pod deletion and pod/PVC status updates remain allowed.
No in-namespace staging Job is allowed: later use the separately trusted
host runner or another explicitly isolated staging location. Allocate the
replacement claim before freeze. No release/unfreeze mechanism exists yet.
Never remove a production policy binding to bypass the barrier.

This is admission-level prevention, NOT physical storage fencing.
API-server caches, already admitted requests, kubelet restarts of existing
containers, existing exec connections, static pods, host root/raw device
access, an unreachable cluster and rollback of its datastore are not
covered. Marker presence and marker-policy dry-run do NOT prove the
workload policy is currently enforced on every API server. The adapter
always reports workloadAdmissionVerified=false, stopEvidenceEligible=false
and activationAllowed=false. Durable host/provider isolation is mandatory.
Existing CRI collector --stop-enrolled now additionally requires
--witness-endpoint, --witness-ca, --witness-cert, --witness-tls-key and
compares the challenge against the current online witness freeze before
requesting a stop. This still cannot prove physical isolation.

CI tests real Kubernetes admission with an enrolled disposable StatefulSet,
real witness signatures and a scoped service account token, attempts
alternate pod access to the old claim, tests immutable marker and
cross-tenant isolation, and verifies StatefulSet recreation rejection.
These are development tests, not production multi-cloud proof.


## Experimental per-PVC LVM isolation (development only)

Local PV directories on a shared filesystem cannot be individually fenced
by making the whole block device read-only: that affects other tenants.
This candidate backend requires a dedicated **linear, single-segment LV per
PVC**, on an independently provisioned shared storage VG. It is not a choice
of final storage filesystem, nor a migration of existing volumes. No thin
pools, snapshots, RAID/cache, multi-segment or shared tenant LV supported.
Each LV is named pvc-<PVC UID> in a gtmd-<pool> VG and tagged
gtmd.tenant.<tenant>, gtmd.role.<role>, gtmd.pvc.<PVC UID>. Provisioning must
independently pin VG and LV UUIDs, machine-id, Kubernetes Namespace UID and
all enrolled PVC/pod/node UIDs in root-owned inventory, and verify the
actual PVC to LV/filesystem/mountpoint relationship. That provisioning and
mount lifecycle is not yet implemented; tags alone are not proof of ownership.

Install local_lvm_fence.py and existing integrity/storage-controller
modules on the trusted host (PYTHONPATH includes both directories), LVM2
and crictl. Example inventory schema:

    {"tenant":"gtm-<32 hex>","role":"sync","namespaceUid":"<UUID>",
     "machineId":"<32 hex>","identity":{"podUid":"<UUID>","nodeUid":"<UUID>",
      "pvcUids":["<UUID>"]},"volumes":{"<PVC UID>":{"vgName":"gtmd-pool",
      "vgUuid":"<LVM UUID>","lvUuid":"<LVM UUID>"}}}

CLI requires root, root-owned non-writable trust files, private token/key,
local CRI socket, witness mTLS credentials and pinned signing public key,
Kubernetes CA/endpoint and token with GET on exactly the tenant namespace.
No kube writes. A host lock excludes concurrent local adapter invocations.
Read-only by default; explicit --apply-development-isolation only on an
approved disposable host. Fetch the current signed witness challenge and
matching immutable namespace marker before each transition; wait out all
prior writer permits. Read LVM metadata and match the exact device-mapper
UUID in sysfs, not an unstable major:minor alone. Require all enrolled
volumes, no live or replacement writer, no process, no mount in visible
namespaces, no open LV and no stacked holder. No forced/lazy unmount and
no automatic removal of a live mount. Existing host filesystem mount must
be cleanly unmounted through trusted lifecycle; busy/unreachable/unknown
state blocks. Never unmount a shared host disk.

Persist read-only LV permission and activation-skip flag via lvchange
with exact VG/LV UUID/name selection, then deactivate and verify metadata
and absence of device-mapper mappings. On partial failure do not undo
restrictions; re-read and retry idempotently. Explicit root override can
reactivate the LV, but persistent read-only metadata remains unless root
changes it. Normal activation should respect skip; verify this in CI.
Reference: https://man7.org/linux/man-pages/man8/lvchange.8.html and
https://man7.org/linux/man-pages/man8/lvs.8.html .

This only protects a specific locally reachable, correctly provisioned
device under the trusted-host assumption. It does not prove durable
provider fencing, rollback resistance of LVM metadata, protection against
raw access to the underlying PV or privileged host/cluster actors, or
prevent empty directory fallback if mount lifecycle is misconfigured.
Neither a namespace marker nor clean CRI sample proves those properties.
A healthy source still needs a separately authorized read-only export
lifecycle; do not apply this adapter to it expecting the existing export
to remain online. There is no unfreeze/activation operation. The adapter
always returns stopEvidenceEligible=false and activationAllowed=false;
never feed this observation to confirm-writer-stop as a stop proof.
CI loop devices are not physical hardware or a two-provider test.


Pre-enrollment read-only lvm_mount_binding.py checks the actual API
Namespace/node/pod/PVC/PV identities, exact Retain local PV path
<managed-root>/<PVC UID>, hostname affinity, RWO Filesystem mode,
non-privileged pod with expected claims, and the actual host mountinfo
entry plus stat.st_dev against the LVM device-mapper identity. Reject
missing/stacked/subdirectory mounts, read-only filesystem and parent
filesystem fallback. Currently candidate ext4/XFS only. No mount creation,
no enrollment action. CLI takes --inventory, --node-name, --mount-root,
--kube-endpoint, --kube-ca, --kube-token. Root-owned independent
configuration and narrowly scoped read-only API credentials required.
Checks are repeated, not an atomic enrollment or runtime mount guard;
the caller must prevent changes during enrollment. Full provisioning
and enforced mount lifecycle remain open.
