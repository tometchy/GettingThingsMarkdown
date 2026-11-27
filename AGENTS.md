# Instructions for contributors and coding agents

## Maintain the big picture with every change

Read [docs/MVP-SPECIFICATION.md](docs/MVP-SPECIFICATION.md) before changing
anything in this repository. It is the authoritative big-picture product
and architecture specification. Also read relevant parts of
[docs/mvp-acceptance.md](docs/mvp-acceptance.md), pricing and component docs.

For **every change**, including fixes, tests, infrastructure, refactors,
documentation and small follow-up PRs:

- Check the impact on the whole product, security model, architecture,
  recovery, costs, dependencies, user experience and MVP requirements.
- Update the relevant sections of MVP-SPECIFICATION.md in the same PR
  whenever a requirement, design, decision, integration, limitation or
  implementation status changes. Reconcile obsolete or contradictory
  text in place; do not merely append a running changelog or duplicate
  the architecture. If no specification change is warranted, explicitly
  explain why in the PR summary.
- Keep docs/mvp-acceptance.md aligned with actual implementation and
  test evidence. Distinguish implemented, tested, partially implemented,
  experimental and planned. Never mark a gate complete because code
  exists or CI was merely started.
- Keep component documentation and pricing assumptions consistent with
  the specification. Do not silently change an agreed requirement,
  safety guarantee, product price or security boundary. Record tradeoffs
  and unresolved decisions; seek owner approval for material changes.
- Before finishing, review the diff against the big picture and state
  what changed, actual test/CI results, and remaining limitations.

## Simplicity, cost and safety

Prefer the simplest sufficient design, existing components and native
platform mechanisms. Maintain two permanent data containers per tenant,
one complete Git repository per replica, no LFS/S3 dependency in the
target MVP and no unnecessary resident services, databases, queues or
third permanent full data copy. Use shared capacity and one-shot repair
where possible. Never trade away data integrity, independent retention,
E2EE, failure-domain independence or safe recovery for lower costs.

No paid resource provisioning, production migration, destructive repair
or unsafe cutover without explicit owner authorization. Keep development
fixtures clearly distinct from production guarantees. Use real tests
where practical, and report failures or missing verification honestly.
