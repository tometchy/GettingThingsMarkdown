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
 Tenant reconciler -> Kubernetes A/B StatefulSets + PVCs
```

- `GTM_DEV_LONG_TRIAL` takes effect only under ASP.NET Development, never based on client device identifiers.
- A month means `AddMonths(1)` rather than 30 fixed days. Trial extensions begin from the later of now or the existing end.
- Trial starts without entering card details. Paid subscription is a separate explicit Checkout action; no silent renewal occurs merely by registering.
- The subscription API is shared across desktop/browser/mobile clients. Browser and desktop use exactly the same Avalonia controls and `AccountApi`; platform-specific code is isolated to host/bootstrap.
- Public HTML landing page supports accessibility, indexing, and fast marketing-page loads; authenticated account UI is Avalonia WASM.
- EasyTools/other providers can be added later behind `IBillingProvider` abstraction (to extract before second provider). EasyTools adds platform fees on top of Stripe processing, so direct Stripe was chosen first.
- Stripe webhooks and tenant entitlement decisions are server-authoritative. The existing tenant controller must enforce quota/expiry in a future hardening pass; currently `CanWrite` is metadata **not an enforcement mechanism**.
- Expired trial: do not delete files automatically; future data plane should block new writes while preserving read/export and a defined grace period.
- Never trust a paid state set from app requests, `checkout.session.completed` alone, or a successful redirect.
- Stripe event recovery / reconciliation, database migrations, email verification, 2FA for admins, audit correlation, refund/dispute handling, retry and duplicate ordering protection, and robust tenancy isolation must be completed before launch.

## Required original MVP work not yet delivered

1. Verify the real-kind two-account and ciphertext-to-Git tests and fix any remaining integration failures.
2. Validate Linux Mint rootless Podman and bundled Syncthing startup on the user's laptop.
3. Implement native incremental history restore of an individual encrypted file using locally held Syncthing credentials; current history API exposes encrypted Git objects only.
4. Add client-side secure recovery and pairing of another trusted device without sharing E2EE keys with the server.
5. Implement storage accounting, quota enforcement, trial expiry and compliant subscription billing.
6. Implement server-identity/config recovery, two protected history copies, delayed Git history rewrite and restore after node/PVC loss.
7. Build and validate embedded native Syncthing engines for Android/iOS, app store billing restrictions, file picker and background lifecycle.
8. Add public TLS ingress, cert-manager, relay, monitoring, Helm/OpenTofu, multi-AZ and disaster recovery.
9. Production email verification, admin 2FA, abuse protection, key rotation and privacy audit.
10. Resolve 200 GiB subscription economics: reserving two paid Scaleway PVCs is much more expensive than a ~4 USD/month plan.

Follow the detailed acceptance requirements in the original project brief; do not pass a mocked service as a working feature.
