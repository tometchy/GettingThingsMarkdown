# Architecture decisions and acceptance gaps

## Account and billing boundary

```text
Shared Avalonia account UI (Browser/WASM, Linux, Windows)
              |
              v
       Control Plane API (.NET 10)
        | SQLite accounts, trial, subscription state
        | StripeGateway (server-only HTTP, no fake payments)
        | Admin trial extension + audit
        v
 [FUTURE: Tenant reconciler -> Kubernetes A/B containers]
```

- `GTM_DEV_LONG_TRIAL` takes effect only under ASP.NET Development, never based on client device identifiers.
- A month means `AddMonths(1)` rather than 30 fixed days. Trial extensions begin from the later of now or the existing end.
- Trial starts without entering card details. Paid subscription is a separate explicit Checkout action; no silent renewal occurs merely by registering.
- The subscription API is shared across desktop/browser/mobile clients. Browser and desktop use exactly the same Avalonia controls and `AccountApi`; platform-specific code is isolated to host/bootstrap.
- Public HTML landing page supports accessibility, indexing, and fast marketing-page loads; authenticated account UI is Avalonia WASM.
- EasyTools/other providers can be added later behind `IBillingProvider` abstraction (to extract before second provider). EasyTools adds platform fees on top of Stripe processing, so direct Stripe was chosen first.
- Stripe webhooks and tenant entitlement decisions are server-authoritative. The future tenant controller must enforce quota/expiry; currently `CanWrite` is metadata **not an enforcement mechanism**.
- Expired trial: do not delete files automatically; future data plane should block new writes while preserving read/export and a defined grace period.
- Never trust a paid state set from app requests, `checkout.session.completed` alone, or a successful redirect.
- Stripe event recovery / reconciliation, database migrations, email verification, 2FA for admins, audit correlation, refund/dispute handling, retry and duplicate ordering protection, and robust tenancy isolation must be completed before launch.

## Required original MVP work not yet delivered

1. kind + rootless Podman and idempotent Kubernetes tenant reconciler.
2. Two separate tenant containers (Syncthing + Git and Gitea + SQLite), separated nodes/PVC.
3. Syncthing device identity persistence, E2EE, pairing/recovery and interoperable tests.
4. Git encrypted working-tree commit + protected push and history restoration.
5. File browser/editor and local Markdown automation engine.
6. Android/iOS embedded Syncthing, platform lifecycle, app store policies.
7. Persistent volume sizing, actual quotas, storage cost evaluation, recovery.
8. Production-grade registration/email/antifraud, backups, secrets and observability.

Follow the detailed acceptance requirements in the original project brief; do not pass a mocked service as a working feature.
