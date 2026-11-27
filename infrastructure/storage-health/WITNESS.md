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
