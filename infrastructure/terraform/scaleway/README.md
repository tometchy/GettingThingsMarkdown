# Scaleway Kapsule production infrastructure

This directory contains **declarative configuration only**. It has not been
applied; executing a plan that provisions a cluster requires explicit approval
and paid Scaleway credentials. The user explicitly prohibited unsolicited paid
cloud deployments.

The module creates a private network, outbound NAT, private-IP-only workers, an API ACL with no public IPs allowed, one Cilium-enabled Kapsule cluster, and
two node pools in **different Availability Zones** with independent bounded
autoscaling and autohealing. Min 1 node per zone is required to satisfy
cross-node hard anti-affinity.

Before any production apply:
1. Run `tofu init && tofu fmt -check && tofu validate` without credentials
   for syntax checking only. `tofu plan` needs properly configured Scaleway
   API credentials. Never commit state or `.tfvars` with secrets.
2. Choose zones, node types and price/capacity. Confirm no minimum node SKU
   limitation and the cluster version supported at deployment time.
3. Build/publish **versioned, signed** control-plane and tenant images to an
   authenticated registry. Never deploy floating `:dev` tags in production.
4. Bootstrap from a private deployment runner; no public SSH, bastion or standing human kubeconfig. Do not assign the owner role to the day-to-day operator. Verify no exec/attach/port-forward, secret read or IAM escalation. A separate cloud organization break-glass identity with MFA and audit can change ACL/IAM if needed. Confirm private API connectivity before applying the deny-all-public ACL. Configure public HTTPS ingress, rate limiting, scoped workloads and Syncthing relay/discovery.
5. Set environment to `Production`, disable the local 100-year trial,
   use managed/sealed secrets, dedicated billing keys and private PostgreSQL
   or fault-tolerant account metadata storage.
6. Choose economically viable, backed-up **two independent** data volumes
   per tenant, storage classes supporting PVC expansion and disaster recovery;
   see `docs/storage-economics.md`.
7. Verify E2EE restoration and failover before taking any real user files.

Sources:
- https://www.scaleway.com/en/docs/terraform/quickstart/
- https://www.scaleway.com/en/docs/tutorials/k8s-kapsule-multi-az/
- https://registry.terraform.io/providers/scaleway/scaleway/latest/docs/resources/k8s_pool

The cost and signed release gates are intentionally not bypassed by this code.
