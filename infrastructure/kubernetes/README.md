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
