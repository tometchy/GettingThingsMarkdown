> **Current design:** New tenants use complete encrypted local Git on BOTH Sync and History; no LFS/S3 required. A seven-day deletion-intent/Undo API and operator incident holds are present, but **physical Git erasure, root-resistant immutability, strict quota accounting, and automatic cross-cloud failover are NOT IMPLEMENTED**. See [full-Git recovery](docs/full-git-dual-cloud-recovery.md) and [history protection](docs/history-protection-operations.md). Admin management at `/admin/` must be private and protected with operator SSO/MFA. No live billing until real cloud and data-loss tests pass.

# GettingThingsMarkdown

A 100% open-source, privacy-first SaaS for Syncthing E2EE, encrypted Git history and Markdown task automation. **Unreleased engineering preview. Not production-ready.**

## Implemented foundation; full-Git dual-cloud recovery engineering preview

- Backend: ASP.NET Core 10 identity, SQLite account/subscription metadata, JWT, password hashing, rate limiting, PL/EN Avalonia shared subscription UI for desktop and WebAssembly.
- Billing: month trial, admin trial extension with audit, Stripe Checkout/Portal and signed webhooks. Server-only 100-year trial when **Development** and explicitly enabled.
- Kubernetes: reconciler creates a separate namespace per account, exactly two tenant StatefulSets (Sync and History), two PVCs, hard inter-pod anti-affinity across nodes, services, credentials, internal TLS and network restrictions.
- Sync tenant: official Syncthing native server in Receive Encrypted mode + separated Git worktree and Git metadata, stabilized automatic commits, push by HTTPS to History.
- History tenant: self-hosted private Gitea+SQLite on separate volume; Git non-fast-forward and delete protection.
- Desktop: Avalonia .NET, native packaged Syncthing process in local profile, E2EE folder password generated on the client, pairing via authenticated API, local-only YAML frontmatter automation that archives completed tasks.
- Shell: rootless Podman + kind with two worker nodes, local image build/load and safe cluster lifecycle.
- Tests: policy/registration/signature tests, tenant-manifest invariants, local archive automation tests, CI builds of all three containers, experimental real-kind two-account and ciphertext/Git end-to-end scripts.

**The affordable-storage extension is experimental and has not been deployed to a paid cloud.** Only claim a test passed after the associated GitHub Actions job actually succeeds. The codebase is currently *in development*, and not every operation described here has passed end-to-end integration.

## Local first run — Linux Mint

Prerequisites: .NET 10 SDK, Podman rootless, cgroups v2, kind, kubectl, curl, jq, openssl and the Linux libraries required by Avalonia. Docker Desktop is not required.

```bash
git clone https://github.com/tometchy/GettingThingsMarkdown.git
cd GettingThingsMarkdown
chmod +x dev.sh
./dev.sh setup
./dev.sh up
```

This starts a development Kubernetes Control Plane at `http://127.0.0.1:5080` through a loopback-only port-forward. All three images are built on the local Podman. The first run downloads container images and may consume substantial disk space. No production cloud resources are created.

Create a test user, or register using `POST /api/auth/register`:

```bash
./dev.sh create-user test@example.com basic
./dev.sh status
./dev.sh integration
```

It takes time for Gitea and Syncthing to initialize. `./dev.sh integration` registers two new accounts and checks isolated workload provisioning and node anti-affinity. For detailed failures use `./dev.sh logs` and `kubectl get pods -A`.

Desktop UI with an embedded, release-verified Syncthing binary:

```bash
bash packaging/linux/bundle-syncthing.sh
./dev.sh desktop
```

Register or log in using the same account as the backend. Wait until the tenant status becomes `Ready`, choose a folder for Markdown, then expose that tenant's Syncthing port via a **separate terminal**:

```bash
./dev.sh expose-sync ACCOUNT_UUID 22000
```

Use `tcp://127.0.0.1:22000` in the desktop client to pair. Never forward the administrative REST API or share its key publicly. The server must not receive plaintext encryption keys. The app's localhost profile stores recovery material locally; back it up securely before trusting the service.

Build the same Avalonia account screens for WebAssembly:

```bash
dotnet workload install wasm-tools
./dev.sh web-publish
```

`web-publish` places assets into the local Control Plane source tree, not inside the running Kubernetes image; to serve `/app/` through the Kubernetes Control Plane container, rebuild and reload that image. Standalone `./dev.sh api` can serve the locally published assets. The public marketing page also exists in `src/ControlPlane/wwwroot/index.html`.

The development backend applies a **100-year trial only when both** `ASPNETCORE_ENVIRONMENT=Development` and `GTM_DEV_LONG_TRIAL=true`. Production accounts must use normal one-month trial; disabling this env flag does not retroactively shorten existing development accounts.

Inspect `infrastructure/kubernetes/README.md`, `docs/desktop-e2ee.md`, `docs/affordable-infrastructure.md`, `docs/storage-economics.md`, and `docs/architecture-and-gaps.md` for limitations.

## Runtime commands

```bash
./dev.sh status                         # cluster pods and PVC
./dev.sh logs                           # Control Plane logs
./dev.sh test                           # .NET unit tests
./dev.sh integration                    # two-tenant integration
./dev.sh down                           # stop API port-forward only, no data deletion
./dev.sh destroy --yes-delete-everything # irreversibly removes kind cluster + data
```

`./dev.sh up` is intended to be re-runnable; `down` is non-destructive. Kind's storage is **not production-grade disaster recovery**.

## Three consumer price tiers (USD, proposal, not active payments)

| Tier | Live encrypted quota | Monthly | Yearly paid upfront | Effective monthly |
|---|---:|---:|---:|---:|
| Mini | 10 GiB | $1.99 | $17.88 | $1.49 |
| Plus | 75 GiB | $5.99 | $59.88 | $4.99 |
| Pro | 500 GiB | $24.99 | $239.88 | $19.99 |

The original 15/200/1024 GiB allowances for **existing** accounts are
preserved by an idempotent SQLite schema migration. Monthly/yearly checkout
price IDs are validated server-side against Stripe's official USD amounts.
More detail, operational caveats and actual profitability scenarios are in
`docs/pricing.md` and `scripts/cost_model.py`. The reduced-volume plans
cannot be sold as unlimited historical archive capacity; backup and Git
retention costs scale with actual version history. No production Stripe
prices have been activated.

## Payment configuration

Stripe is **directly** integrated (not via EasyTools). To test real Stripe sandbox workflows, configure in the backend environment: `GTM_STRIPE_SECRET_KEY`, `GTM_STRIPE_WEBHOOK_SECRET`, `GTM_STRIPE_PRICE_BASIC`, `GTM_STRIPE_PRICE_PLUS`, `GTM_STRIPE_PRICE_PRO`, `GTM_PUBLIC_URL`. Never put production keys into repo, client, Docker image or CI logs. Webhook events: `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.paid`, `invoice.payment_failed`. Payment redirects alone never authorize access.

Before charging users, complete Stripe reconciliation, pricing/VAT, email verification, storage enforcement, cancellation and grace periods, and app-store billing policy validation. Existing subscriber quota migration is tested in `tests/ControlPlane.Tests/LegacyQuotaMigrationTests.cs`.

`/api/plans` is the canonical machine-readable public catalog; the one-calendar-month trial is represented as `trialMonths=1`. Annual Checkout reuses the same account and API as monthly billing. Stripe checkout verifies live Price objects against the configured USD totals before redirecting.

## Release blockers

- **E2EE/Git recovery:** Offline recovery from full Git is tested in CI, but automatic fresh-PVC restoration after a real cloud loss and end-user encrypted-history restore UX are not implemented. The new history erasure API records requests only; no physical erasure occurs.
- **Tenancy and quotas:** No production-complete physical quota enforcement; current PVC sizes are reservations, not user quotas. Fresh-password deletion intents and hold extension are available, but independent immutable retention, safe dual-Git purge and audited physically verified cleanup remain mandatory release gates.
- **Syncthing:** Browser cannot run the native filesystem daemon. Android and iOS platform-native bridges, secure storage, background execution and Apple file integrations still needed; iOS background limitations cannot be removed with a shared UI.
- **Infrastructure:** TLS public ingress, cert-manager, production relay/discovery, monitoring, backups, failure/recovery automation, production Scaleway Terraform/OpenTofu and Helm not complete.
- **Product:** Markdown explorer/editor/restore UX and advanced automations incomplete; email verification and account recovery absent; security review required.
- No live Stripe charge or paid cloud provisioning has been performed.

This project cannot be presented as a complete or production-ready MVP until these requirements pass acceptance tests.

## License

Own source files are released under MPL-2.0; dependencies retain their licenses. Full dependency and packaging audit is pending.
