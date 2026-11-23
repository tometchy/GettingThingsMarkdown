# Kapsule storage cost — architecture review

Checked 2026-11-23 against official Scaleway pricing:
https://www.scaleway.com/en/pricing/storage/

Block Storage 5K (Paris): **€0.000130 per decimal GB per hour**, before tax.
15K: €0.000177 per decimal GB per hour. These are provisioned Kubernetes
Block Storage volumes, **not** Object Storage rates. Charges depend on exact
cloud plan, zone, time, region and provider.

The current tenant controller reserves **two** PVCs sized to the subscribed
plan, with Git object history and current encrypted files on A plus a second
independent history copy on B. It therefore provisions twice the nominal
storage capacity. The following uses a 730-hour month and converts GiB to
decimal GB, with no compression or overcommit assumptions.

| Plan | Two PVCs provisioned | 5K monthly cost | 15K monthly cost |
|---|---:|---:|---:|
| Basic | 2 × 15 GiB | €3.06 | €4.16 |
| Plus | 2 × 200 GiB | €40.76 | €55.50 |
| Pro | 2 × 1024 GiB | €208.69 | €284.14 |

This excludes nodes, load balancers, bandwidth, Stripe fees, backups,
monitoring, taxes and operations. History growth may require further storage.

**Decision needed before charging customers:** a $4/month Plus plan is not
profitable with 2 × 200 GiB reserved Kapsule block volumes. Options include
an initial small PVC that grows with actual usage (still expensive for heavy
users), moving A/B onto cheaper independent HDD hosts, choosing a different
storage substrate while preserving two isolated copies, or changing pricing.
Do not silently discard the two-copy security requirement to make the model
work. Benchmark and model realistic user distributions before committing.
