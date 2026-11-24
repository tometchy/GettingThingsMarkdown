# Git-only incoming mail

The native Syncthing receiveencrypted folder is not an arbitrary ciphertext store. No direct writes to it and no second Syncthing identity. SMTP receives MIME (temporarily plaintext), encrypts it with AES-256-GCM and wraps the AES key for each registered independent RSA device public key. Private device keys stay on clients.

The backend commits one encrypted JSON envelope at .gtmd/inbox/<digest>.json to the gtm-mail branch of the existing tenant Git repository on A; pushes that exact ref to History on B before SMTP success. It does not check out the mail branch or write the Syncthing directory. Retries reuse the existing envelope. New SQLite rows are metadata for duplicate/rate accounting, not a mail queue or a source of new payloads. Legacy SQLite ciphertext remains readable; it never expires automatically.

The client reads /api/history/mail (paginated Git tree, pinned to one revision) and /api/history/mail/<digest>?revision=<sha> (raw Git file via existing tenant read-only Gitea credentials). It writes Markdown and extracted attachment bytes into its ordinary local workspace after authenticated decryption. A synchronized .gtmd/imported receipt avoids repeating an import; the source remains in Git. Manual restoration can recreate missing tasks. Concurrent devices use deterministic paths and non-overwriting writes; distributed exactly-once delivery is not claimed. The complete original MIME stays encrypted in Git, including attachments. No automatic remote image loads. 40 MiB raw SMTP limit.

The mailbox public-key registry and alias are age-encrypted in .gtmd/settings.json.age on gtm-mail when independent recovery recipients are configured. No private device key is included. Later-added keys cannot decrypt older envelopes without an existing private key or future rewrap support.

Production access: private Scaleway workers, outbound NAT, no public Kubernetes API, dedicated observability-only IAM role. No standing owner/root credentials for daily operation; cloud owner can explicitly escalate. No resources applied. Real provider IAM validation, cross-cloud failover, recovery of control-plane metadata, mobile clients, key revocation, physical protected purge and hard quotas remain release gates.

## Revision: Git is also the delivery ledger

New Kubernetes mail deliveries no longer create SQLite receipt/deduplication rows. The single tenant Git writer uses the deterministic digest filename and recent Git commits touching the inbox path for the 100/day cap, checks capacity and acknowledges only after successful replication. Retries re-push the previously committed envelope without consuming another daily slot. Existing legacy rows are retained, not deleted. Account and alias/public-key lookup still use the shared control-plane registry with an encrypted Git backup. The Git writer's private HTTP interface uses explicit Content-Length.
