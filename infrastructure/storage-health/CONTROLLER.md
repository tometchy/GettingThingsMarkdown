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

Explicit storageController.reserveAvailable=true grants cluster-scoped PV
patch permission and enables reservation of **only** unclaimed Available
Retain managed local PVs whose mapping or health is unverified. JSON Patch
tests resourceVersion, UID, phase, Retain and reserves via claimRef to an
intentionally nonexistent PVC in gtm-storage-quarantine. Restrict creation
of PVCs in that namespace to nobody during operation. This is standard
Kubernetes prebinding, not deletion; never auto-unreserve even if SMART
recovers. ResourceVersion test protects against simultaneous binding; retry
from new inventory, not stale patch. The API RBAC cannot constrain patch
fields or PV classes; a production validating admission policy, real CNI/RBAC
audit, and real race test are required before enabling this write mode.
The controller does not stop existing writers, scale workloads, delete
claims or change retention. Bound volumes require a verified recovery
authorization and coordinated fencing before movement. Local PV discovery
and allocator must coordinate to prevent new PV creation on suspect disks;
periodic reservations do not guarantee zero admission race. No live cutover.

No paid resources started; test only with kind and synthetic host reports.
