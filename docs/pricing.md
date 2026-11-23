# Public pricing proposal (23 November 2026)

Public prices in **USD**, monthly or prepaid 12 months; VAT treatment depends
on the buyer's country. Stripe must be configured with six valid, recurring
Price IDs whose currency, interval and amount match the server-side catalog.

| Plan | Encryption and Git/history | Live quota | Monthly | Annual invoice | Equivalent/month |
|---|---|---:|---:|---:|---:|
| Mini (`basic` API id) | Included | 10 GiB | $1.99 | $17.88 | $1.49 |
| Plus | Included | 75 GiB | $5.99 | $59.88 | $4.99 |
| Pro | Included | 500 GiB | $24.99 | $239.88 | $19.99 |

One-month free trial without payment card at registration. Optional extension
by administrator; on Linux Mint developer-only 100-year trial requires both
Development environment and explicit `GTM_DEV_LONG_TRIAL=true`. Monthly and
yearly recurrence comes from the **same** account/Stripe backend and Avalonia
desktop/WebAssembly UI. The backend resolves plan amounts, not user input.

IDs intentionally remain `basic`, `plus`, `pro` for compatibility with old
users and Stripe subscriptions. Existing SQLite accounts are migrated to
`QuotaOverrideBytes` with the old 15/200/1024 GiB allowance; their existing
quotas are preserved even when the newly advertised tiers shrink. New accounts
inherit 10/75/500 GiB. Migrating existing paid users to new prices requires
explicit consent, renewed Stripe price agreements and storage entitlement
reconciliation — not automatic reduction.

**Competitive reality:** Filen Pro I offers 200 GiB for €1.99/month and
Syncthing Cloud ST-200 offers 200 GB for $5/month. This product cannot
compete on raw capacity at identical price. Its differentiated product is
Syncthing E2EE together with retained independent encrypted Git history and
WORM snapshots (when that feature passes full S3 production testing).
Sources: https://filen.io/pricing and https://syncthing-cloud.com/.

**Not yet a verified profitable offer.** The two-cloud LFS path now keeps all
historical encrypted LFS bytes locally on Sync *and* via the remote Gitea/S3.
Sync therefore reserves a **2× plan quota PVC** (Mini 20 GiB, Plus 150 GiB,
Pro 1000 GiB), with a separate 8 GiB History metadata PVC. This is a
reservation, not a hard history limit: unbounded Git history may exceed it.
Previous 1× Sync HDD margin estimates were optimistic; a new history-quota,
7-day deletion/undo and measured costs are still mandatory.

**Not yet a verified profitable offer.** Two always-resident containers per
customer consume CPU/RAM. 10/75/500 GiB pricing is an initial *scenario*,
not a guarantee of profit at small scale. The server pod requests are
25m CPU/128MiB per Mini container, 25m/192MiB per Plus container, and
100m/256MiB per Pro container with independent higher burst limits. These
are scheduling assumptions, **not observed measured idle memory**.
Provisioning per tenant still needs verified HDD CSI with hard block quotas
and separate failure domains, independent S3 versioning/retention, and
end-to-end recovery including Git LFS and identity/config backups.

Production charging must remain disabled until published price IDs, taxation,
actual node/hard-disk invoices, storage utilization, load testing, Stripe
reconciliation, refund and cancellation policies, and mobile purchase rules
have been verified. Use Stripe's test mode first; never commit keys to Git.
