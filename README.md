# GettingThingsMarkdown

Open-source, E2EE-first Markdown synchronization SaaS. **This is an early vertical slice, not the complete MVP and not production-ready.**

## Implemented in this branch

- ASP.NET Core 10 Control Plane prototype using SQLite for **account and subscription metadata only**.
- Register/login API with salted password hashing, signed bearer tokens and basic IP rate limiting.
- Plan catalog: Basic 15 GiB, Plus 200 GiB, Pro 1 TiB (quota metadata only; enforcement pending).
- 1-calendar-month free trial, no credit card; dev-only opt-in 100-year trial.
- Separate administrator secret for audited trial extension.
- Stripe Checkout subscription sessions, Stripe Billing Portal, signed webhook processing, idempotency by Stripe event ID.
- Shared Avalonia UI library and desktop + browser/WASM hosts with PL/EN account screens.
- Initial marketing page and CI workflow.

**Not implemented:** provisioning, kind/Podman, Syncthing, E2EE, Git history, Kubernetes, mobile runtimes, actual quota enforcement, outbound email verification, account recovery, password resets, deployment/migrations, and release hardening. No user file is stored by this code. No live payment was processed or tested against Stripe. The public site links to `/app/`, which requires publishing the browser output to that path.

## Run locally (Linux Mint)

Prerequisites: .NET 10 SDK and Avalonia's Linux system libraries. For the browser host install `dotnet workload install wasm-tools`.

```bash
git clone https://github.com/tometchy/GettingThingsMarkdown.git
cd GettingThingsMarkdown
# Check out the PR branch until merged:
git switch feature/subscriptions-trial-web-avalonia

export GTM_JWT_SECRET="$(openssl rand -hex 32)"
export GTM_ADMIN_TOKEN="$(openssl rand -hex 32)"
export GTM_DEV_LONG_TRIAL=true
./dev.sh api
```

The local API listens at `http://localhost:5080`. Keep it on loopback. In another terminal run `./dev.sh desktop`. For the WWW account page, first run `dotnet workload install wasm-tools` followed by `./dev.sh web-publish` (this copies the Avalonia Browser output to the Control Plane static folder); then open `http://localhost:5080/app/`. Both the WWW and desktop app use the same account UI and API client. `./dev.sh browser` is an isolated browser development server and needs API reverse-proxying for sign-in.

Example API operations:

```bash
curl -sS http://localhost:5080/healthz
curl -sS -H 'Content-Type: application/json' \
  -d '{"email":"demo@example.com","password":"use-a-long-password-here","plan":"plus"}' \
  http://localhost:5080/api/auth/register
```

The registration response includes an access token and the server-calculated trial expiry. Pass `Authorization: Bearer <token>` to `GET /api/account`.

To find an account as administrator, use `GET /api/admin/accounts?email=demo@example.com` with header `X-Admin-Token`. To see its audit log, use `GET /api/admin/accounts/ACCOUNT-UUID/audit` with the same header.\n\nTo extend an existing trial, use the account UUID from the registration response or the admin lookup:

```bash
curl -X POST "http://localhost:5080/api/admin/accounts/ACCOUNT-UUID/extend-trial" \
  -H "X-Admin-Token: $GTM_ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"days":30,"reason":"support request"}'
```

The 100-year trial is calculated **only on the backend** when the ASP.NET environment is `Development` and `GTM_DEV_LONG_TRIAL=true`. The desktop app cannot extend production accounts, irrespective of the machine it runs on. Never enable Development in a public deployment.

## Stripe configuration

Configure `GTM_STRIPE_SECRET_KEY`, `GTM_STRIPE_WEBHOOK_SECRET`, `GTM_STRIPE_PRICE_BASIC`, `GTM_STRIPE_PRICE_PLUS`, `GTM_STRIPE_PRICE_PRO`, and `GTM_PUBLIC_URL` on the **server**. These keys must never be shipped in client binaries or committed. Use Stripe **test mode** first. Subscribe webhook endpoint `POST /api/webhooks/stripe` to `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.paid`, and `invoice.payment_failed`. Customer billing state is updated by signed webhooks, not success-page redirects. Only Stripe controls prices; user-supplied plan IDs map to server-configured Price IDs.

Webhook event ordering and paid entitlement reconciliation require further integration and adversarial testing before collecting real money. Accounting/tax/VAT and PSD2/SCA obligations also require review. The system is not yet wired to stop tenant writes when an entitlement expires.

### App store distribution

Apple and Google Play have distinct rules for selling cloud-storage/digital subscriptions inside apps. A common account UI does not imply that an in-app Stripe Checkout button is permitted in all store builds. Keep payment launch policy platform-specific, and check relevant regional exceptions before distributing Android/iOS builds.

## Testing

```bash
./dev.sh test
dotnet build src/Client/Desktop/GettingThingsMarkdown.Client.Desktop.csproj
dotnet workload install wasm-tools
dotnet publish src/Client/Browser/GettingThingsMarkdown.Client.Browser.csproj -c Release
```

Tests cover trial dates, dev gating, administrative calculations, Stripe webhook signature validation, and isolated account registration. GitHub Actions builds the backend/desktop and browser. **No successful test runs are claimed before CI verification.**

## Licensing

MPL-2.0 applies to our own code. Components retain their respective licenses. A dependency/license audit is required prior to release.
