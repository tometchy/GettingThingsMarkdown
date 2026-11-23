# GettingThingsMarkdown Helm packaging

This chart configures **only shared** Control Plane components, not a third
container for any tenant. The single shared reconciler creates per-user A/B
StatefulSets and PVCs as specified in source code.

For local preview **without deploying**:

```bash
helm lint infrastructure/helm/gtm-controlplane
helm template gtm infrastructure/helm/gtm-controlplane
```

For production, first create a Kubernetes Secret `controlplane-secrets` in
`gtm-system` with random `jwt` and `admin` strings. Use a managed secret
provider rather than chart values. Publish three version-pinned images to an
appropriate registry. Supply real DNS and configure ingress-nginx,
cert-manager and a functioning ClusterIssuer; this chart does not create a
ClusterIssuer, external DNS or a Load Balancer by itself.

Example **render only**:

```bash
helm template gtm infrastructure/helm/gtm-controlplane \
  --set image.repository=registry.example.invalid/gtm/controlplane \
  --set image.tag=reviewed-sha \
  --set syncImage=registry.example.invalid/gtm/sync:reviewed-sha \
  --set historyImage=registry.example.invalid/gtm/history:reviewed-sha \
  --set api.host=api.example.invalid \
  --set api.ingressEnabled=true \
  --set api.issuerName=letsencrypt-prod
```

**No Helm install or Scaleway Terraform apply has been performed.**
Production authorization, quotas, storage costs, observability, image
signing and disaster recovery are still blockers. Helm values default to
production (no 100-year trial) and public ingress disabled.

## Email and Stripe integration

Mail and billing credentials are injected from **existing Kubernetes Secrets**,
never from Helm values or repo source. In Production, an HTTPS
`GTM_PUBLIC_URL` and SMTP configuration are mandatory: the API refuses to
start without them, and an account does not provision a tenant until the
email link is opened. The development environment auto-verifies local users
without SMTP and still gates the 100-year trial on `GTM_DEV_LONG_TRIAL`.

Create two Secrets through your enterprise secret operator or your private
cluster admin console:

- `billing.existingSecret` with `stripe-key`, `stripe-webhook-secret`,
  `price-basic-month`, `price-basic-year`, `price-plus-month`,
  `price-plus-year`, `price-pro-month`, `price-pro-year`.
- `mail.existingSecret` with `smtp-host`, `smtp-port`, `smtp-username`,
  `smtp-password`, `smtp-from`.

Prices must match `docs/pricing.md` exactly in USD. All validation is
server-owned. Never paste secrets in Helm --set, terraform files, GitHub PR,
or command-history entries. Validate billing in Stripe test mode before live.
