# Recovery witness (reference implementation, not live fencing)
Independent trust domain, separate from A and B, only recovery metadata.
No tenant contents, no storage credentials, no third persistent data copy.
Run Python 3 + OpenSSL with a root-owned private 0700 state directory,
separate pinned Ed25519 keys for witness and each replica agent, config
managed by trusted provisioning. Config schema:

    {"tenants":{"gtm-<32 hex>":{"sync":{"cloud":"a","publicKey":"/keys/a.pub","sourceUrl":"https://a.internal/repo.git"},"history":{"cloud":"b","publicKey":"/keys/b.pub","sourceUrl":"https://b.internal/repo.git"}}}}

Do not place the witness key or the independent replica evidence keys in
tenant workloads. Commands in server/integrity/recovery_witness.py:
challenge anchor; collect replica_evidence.py attest on stable snapshots in
both clouds; anchor with both signed files. A challenge is single-use,
short-lived and bound to the previous generation/ref map. Both agents
verify full Git and append-only ancestry. Non-head refs cannot be rewritten
or removed. No purge support. The witness serializes each tenant transition
with flock, atomic file replace and fsync of file and directory, and signs
the monotonic generation record. Ref maps are metadata only. No implicit
reset on invalid state. Restore from an old witness backup is not safe:
independent anti-rollback storage/watermark and HA remain required.
 
For repair: challenge repair --target-role sync --target-pvc <uid>;
collect signed history evidence matching the anchor; start-repair; ticket
<operation> <new-output-directory>. The ticket matches authorized_rebuild.py
and pins all refs and target identity. Staging must occur on a verified
new volume, never an existing live repo. The target trusted agent signs
the receipt after independently verifying its provenance. The witness
requires physicalScrub=true and matching ticket digest/identity. It records
staged_verified, never activation. Repeated issue before expiry returns the
same ticket; after expiry a new attempt is generated. Abort only after ticket
expiry. The reference CLI is for trusted orchestration; no remotely exposed
API, actual agent authentication channel, external monotonic watermark,
automatic ticket worker or storage/job binding yet. A ticket expiry governs
start, not guaranteed termination of an in-flight rebuild. Do not activate
or reuse a staging target while a job may still be running.
 
The challenge expiry is currently 10 minutes, including a full local scan;
large histories need a separately authenticated snapshot/scan protocol with
measured freshness, not a weakened signature check. The witness deliberately
freezes generation advancement during active repair (not actual writers).
Actual writer enforcement, quorum during partitions, independent retention,
provider fencing, stable snapshot, correct full-Git branch consolidation,
and verified cutover are still mandatory. This CLI does not authorize writes
or make the service production ready.
