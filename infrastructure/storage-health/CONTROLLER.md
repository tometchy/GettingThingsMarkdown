# Shared storage controller (first operational increment)

The host probe runs independently of Kubernetes workloads. The optional host
publisher sends only opaque disk IDs, state, and original sample time to one
pre-created ConfigMap in gtm-storage-reports. Give each host a short-lived,
renewed token scoped to its own resourceName (example YAML), a pinned CA, and
its real node name/UID; install the three Python files from
server/storage-controller on the host under /usr/local/lib/gtmd/storage-controller,
and the report systemd service/timer. EnvironmentFile variables GTM_NODE,
GTM_NODE_UID and GTM_API_ENDPOINT point to the real private HTTPS API. Do not
use an admin kubeconfig, or allow tenant pods to write reports. Rotate/revoke
the node credential on replacement. No serial or path is sent to the API.

The independently provisioned inventory in the chart namespace is a ConfigMap
with key inventory.json (see example). It lists expected managed local storage
classes, node names, Kubernetes UIDs, hostname labels, provider identity and
opaque disk ID→mount root. Derive disk ID from the host probe, verify mount
identity during provisioning, and annotate **each** managed local PV:
gtmd.io/node, gtmd.io/node-uid, gtmd.io/disk-id. Require Retain and exact
single-term hostname node affinity, local.path strictly under its registered
mount. These declarations are not a proof of physical mount identity; the
trusted allocator/host mounting process must verify real devices and quotas.
Never permit tenant identities to create or annotate these PVs.

Helm storageController.enabled=true + inventoryConfigMap enables one shared
controller per cluster. Audit-only default has no PV patch permission. It
lists nodes, PVs, PVCs and dedicated reports, considers missing/stale/replaced
node or disk UNKNOWN, detects affected bound tenant claims, emits repair
proposals and safe candidate names (not capacity-validated allocations).
Logs include opaque tenant namespace/PV IDs; keep private.

Explicit storageController.reserveAvailable=true plus managedClasses allowlist grants cluster-scoped PV
patch permission and enables reservation of **only** unclaimed Available
Retain managed local PVs whose mapping or health is unverified. JSON Patch
tests resourceVersion, UID, phase, Retain and reserves via claimRef to an
intentionally nonexistent PVC in gtm-storage-quarantine. Restrict creation
of PVCs in that namespace to nobody during operation. This is standard
Kubernetes prebinding, not deletion; never auto-unreserve even if SMART
recovers. ResourceVersion test protects against simultaneous binding; retry
from new inventory, not stale patch. The optional chart installs Kubernetes v1 ValidatingAdmissionPolicies (requires Kubernetes >=1.30 with feature enabled): restrict controller updates to Available unclaimed Retain local PVs in the explicitly configured classes, require the specific reservation claimRef and unchanged storage properties/metadata, and deny PVC creation in the quarantine namespace. RBAC alone cannot constrain patch fields. Audit CEL compatibility, role scopes and real race behavior before enabling write mode. Deploy admission policies and wait for enforcement before granting the controller patch access in production; chart install order is not a fencing guarantee. Never remove the policies while the controller can patch PVs.
The controller does not stop existing writers, scale workloads, delete
claims or change retention. Bound volumes require a verified recovery
authorization and coordinated fencing before movement. Local PV discovery
and allocator must coordinate to prevent new PV creation on suspect disks;
periodic reservations do not guarantee zero admission race. No live cutover.

No paid resources started; test only with kind and synthetic host reports.


## Verified staging and physical read

Both tenant images include authorized_rebuild.py and physical_scrub.py.
The former verifies an Ed25519 signature against an independently provisioned
pinned public key (OpenSSL). A short-lived ticket binds tenant, current
generation, source/target role/cloud, target PVC UID, operation ID, source
HTTPS URL and exact complete ref map. The caller must supply the current
generation and target identity from an authenticated external coordinator,
not from the ticket itself. All four logical branches are mandatory; a legacy
split recovery repository fails closed. Fetch into a unique fresh staging
directory, no redirects, no hardlinks/alternates; verify full Git and exact
refs. On success a receipt is written with activationAllowed=false.
No signer/witness, fencing, verified target mount binding, automatic job
or publication is implemented. Reflog-only retained data must first be
rooted via protected refs. This is a staging primitive, not live failover.

physical_scrub.py requires a stable repository (offline or trusted snapshot).
It enumerates HEAD, packed refs, objects, refs and reflogs, checks regular
files and stable inode/stat metadata, compares SHA256 of aligned Linux
O_DIRECT reads against buffered reads, then runs Git fsck/verify-pack.
Unsupported O_DIRECT fails closed; never silently fall back. No guarantee
of bypassing drive/controller caches. This reads files at least twice
and Git may read again; benchmark throughput. A live repository can
change during scan: detected changes invalidate the result, but this is
not a replacement for an actual snapshot or writer lease. Do not claim
a 24/48h physical SLO yet. No filesystem scrub scheduling, encrypted
working-tree verification or node repair cutover yet.

## Controller observability

Private Service storage-controller-metrics in the configured chart namespace,
port 8391. Scrape as job gtmd-storage-controller from the authorized
monitoring namespace/pod. Load controller rules only where the optional
controller is expected; absent(up) intentionally alarms when it disappears.
Metrics are aggregate counts and a reconciliation freshness gauge, never
filenames or credentials. The kind CI fixture uses synthetic PVs and a real
API server/binder to test reservation and bound-claim preservation.

Before each write the controller dry-runs a deliberately forbidden reservation
variant and requires the specific admission-policy denial from the real API.
If the policy is missing, the response is ambiguous or the PV has raced a
binder, no real mutation is attempted and observability reports manual review.
The real reservation retains its CAS. This is a runtime guard, not a guarantee
against a cluster administrator removing the policy between requests.

## Working-tree verification

Sync image includes worktree_integrity.py. It requires a consistent offline
snapshot or independently fenced directory and the expected main OID from
the trusted coordinator. It streams NUL-delimited Git tree entries into a
private temporary SQLite index (only ephemeral path metadata), hashes the
actual ciphertext file bytes with Git's blob format, detects missing, extra,
changed or nonregular entries, and validates all Git refs/objects. No path
names are logged. It intentionally refuses unknown extra files including
Syncthing technical files; a future snapshot/provisioner must explicitly
validate those before excluding them. It never resets a live working tree or
mistakes uncommitted user changes for permission to repair. Integrating it
with continuous scheduling and stable snapshots remains a release blocker.


## One-shot repair allocator: freeze first, use the native binder

No new daemon, controller loop, reservation database, queue, CRD or tenant
container. Run server/storage-controller/repair_allocator.py as a trusted
one-shot task when reconciling an incident, with PYTHONPATH containing
server/integrity and server/storage-controller. Uses existing node/disk
inventory and scoped health reports. A common spare pool, not one spare per
tenant; capacity reserve still must cover the largest failure domain under
the governing SLO. Do not allocate paid machines automatically.

Enable repairAllocator.enabled=true with a dedicated
repairAllocator.storageClass in Helm (requires the existing
gtm-storage-reports namespace). Include that class in independent storage
inventory and the storage controller's managed classes. The class must
have provisioner=kubernetes.io/no-provisioner and
volumeBindingMode=Immediate. Prepare fresh empty, capacity-enforced local
Retain RWO Filesystem PVs with gtmd.io/repair-spare=true label and existing
independent node/disk mapping. Do not reuse Released PVs or clear claimRef
as an allocation strategy. Physical initialization and mount ownership
verification are separate gates; this adapter does NOT provision disks or
format volumes. The candidate LVM layout needs a verified spare-slot
mount/PVC association after binding; no automatic LVM mount is implied.

Helm creates only namespace, RBAC and native admission policies; no
Deployment or additional compute. The dedicated actor gets GET on
nodes/PVCs, GET/LIST PV metadata, GET the designated StorageClass and
reports, CREATE PVC only in gtm-recovery. No PV patch, claim delete,
pod creation or tenant writes. Use short-lived TokenRequest from trusted
coordinator. Admission blocks pods in gtm-recovery, other claims consuming
the dedicated repair class, creation by other identities and mutating
the staging claim's binding/incident or deleting it. Native binder may
update its ordinary metadata/status. Policies are not protection from
cluster administrators who can remove them. The actor is trusted to request
the correct incident, can consume spare capacity if compromised, but has
no data plane or live activation permission.

CLI arguments: --inventory, --repair-class, --tenant, --incident, --role,
--kube-endpoint, --kube-ca, --kube-token, --witness-endpoint, --witness-ca,
--witness-cert, --witness-key, --witness-public. Read-only default.
--apply-development-allocation first freezes enrolled writers through the
witness, even when no spare exists, then verifies the signed current
challenge. Without an anchor, stop safely. Verify old primary role claim
UID and binding, original node/cloud and other role's independent cloud.
Filter pre-created Available unclaimed Retain PVs for enough capacity,
compatible mode, no overlapping local paths, fresh healthy reports, a
distinct eligible same-cloud node/disk. Best-fit capacity, stable name
tie-break, one health read per candidate node. Native binder arbitrates
races; explicitly pin volumeName, class, spare selector and target PV UID
in an incident-specific PVC (repair-<freeze digest prefix>). A lost
create reply or retry reads that same claim. No automatic retarget,
deletion or release on conflict or stale health; safe blocked state
requires trusted incident review. No dynamic fallback.
Only after exact bound PVC/PV UID and fresh eligibility verification,
attach one target UID to the existing witness freeze using its existing
transaction/CAS. This does not change the signed challenge, epoch,
prior permits' deadline or collected stop proofs. The old target and
old live claim are not modified. Allocation is for the primary Git
volume only; separate sync-live reconstruction, physical binding,
staging runner invocation, provider fence, independent retention and
cutover remain open. No authorized activation is returned.

The staging claim lives in gtm-recovery, intentionally separate from
the immutable frozen tenant namespace. Host staging must verify its
actual mount and not start pods there. Do not remove admission policy
or delete/rebind the claim for cutover; later implement a verified
replacement enrollment/cutover transaction first. This currently
preserves a safe blocked state rather than pretending to repair live data.
