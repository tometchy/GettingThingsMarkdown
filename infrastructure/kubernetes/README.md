# Local Kubernetes prototype

Run `./dev.sh setup` to check prerequisites, then `./dev.sh up`.
This creates a `kind` cluster named `gtm` on **rootless Podman** with two workers.
The shared control-plane reconciles authenticated accounts into isolated Kubernetes
namespaces. Every tenant has **exactly two** StatefulSets (Sync + History), their
own PVCs and credentials. A hard anti-affinity requirement separates the two
pods across nodes.

`./dev.sh down` stops local port-forward processes without deleting data.
Destroying the cluster and PVCs requires `./dev.sh destroy --yes-delete-everything`.

This is developer infrastructure, not production:
- No public HTTPS ingress, cert-manager or production relay is deployed.
- Gitea uses an internally pinned, per-tenant self-signed TLS certificate.
- Syncthing is accessed through a private Service and can be port-forwarded
  for manual client testing; `kubectl port-forward` is not production discovery.
- SQLite and PVCs inside kind are not independently backed up.
- `dev.sh up` may fail if rootless cgroup delegation or storage drivers are
  misconfigured; the script reports the required checks rather than altering
  your machine automatically.
- Applying a new image requires rebuilding and reloading it into kind.
- A PVC sized 2Gi is deliberately used locally regardless of plan, to avoid
  consuming hundreds of gigabytes on a laptop. This is **not** enforcement of
  the subscribed logical quota. The production size comes from the plan.
- No claim of tested E2EE interoperability until the acceptance tests pass.

## Low-cost encrypted LFS archive (optional, not enabled in kind)

Set `GTM_GIT_LFS_ENABLED=true` on the Control Plane and supply a **dedicated,
restricted** Hetzner Object Storage bucket/key per tenant as Kubernetes Secret
`gtm-lfs-credentials` in each `gtm-<account-id-without-hyphens>` namespace.
Keys: `endpoint` (e.g. `fsn1.your-objectstorage.com`, **no scheme**),
`access-key-id`, `secret-access-key`, `bucket`, `location` (e.g. `fsn1`).
Provision only after the namespace exists. Pre-create bucket/key out of band;
no global full-access S3 credential is ever shipped inside a tenant workload.
The reconciler will **fail closed** until the required Secret exists.

In LFS mode, the Sync/Git container stores its active encrypted Syncthing
files on a local PVC and stores encrypted historical objects through Gitea's
Git LFS driver on S3. Gitea keeps SQLite, refs, commit metadata and LFS
pointers on its own much smaller PVC. This reduces the need for a second
full-size *block* volume. It does NOT reduce the live Sync PVC size, and
does NOT make S3 an appropriate POSIX filesystem for Syncthing or SQLite.

The local kind example keeps LFS disabled to make the transfer test independent
of paid object-storage credentials. Before enabling in production:
- Verify the Gitea S3 endpoint and object store access, quotas and versioning.
- Do not activate LFS on an existing Git repository without a migration:
  historical blobs need `git lfs migrate` or remain on the Git PVC.
- Snapshot both Gitea metadata and encrypted LFS objects consistently.
- Back up the Syncthing device identity/config and Git data independently.
- Add dedicated 30-day WORM backup (Object Lock in COMPLIANCE mode) with
  a separate writer restricted to uploading, not deleting.
- Use a cheap local HDD CSI/storage class **with real quota enforcement** for
  the large active Sync PVC. Default Scaleway SSD block storage will still
  be too expensive for a 200 GiB $4 plan.
- Hetzner Object Storage has a bucket-count limit and per-key permissions.
  Dedicated buckets per tenant will require a scaling or IAM strategy.

LFS uses `[storage] STORAGE_TYPE=minio` (Gitea 1.27 semantics; supports S3)
which places Gitea attachments and other non-SQLite blobs in the same scoped
bucket. No unencrypted tenant files are sent to it.
