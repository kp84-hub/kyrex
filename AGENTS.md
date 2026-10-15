# Kyrex product and implementation direction

Kyrex is being developed as a general agent platform for multiple users,
models and interfaces. Personal examples such as Level 6 workouts are acceptance
cases for reusable capabilities, not the default product model.

Read [the platform architecture](docs/architecture/universal-agent-platform.md)
and [the integration audit](docs/architecture/integration-audit.md) before
changing connectors, routing, approvals, background execution or device access.
They distinguish existing behavior from proposed work; documentation does not
grant runtime authority or mean that a migration has shipped.

## Implementation rules

- Reuse the Python engine, existing policy evaluation, CloudTaskStore/Flux and
  established provider boundaries. Extend them rather than adding another agent
  loop, permission engine or per-feature background queue.
- Keep Bot personality, model selection and task routing separate from account
  ownership and capability authorization. Preserve explicit Bot restrictions.
  A name, role, prompt, model or delegation cannot grant access.
- Put provider protocols and site-specific parsing in adapters. Put studios,
  recipients, timezones, preferred times, account selection and destinations in
  validated owner configuration. Do not add personal defaults to shared runtime.
- Surface adapters submit and observe the same task. Closing a tab, losing a
  voice connection or changing devices must not imply cancellation or resubmit
  an accepted effect. Make explicit cancellation a separate action.
- Bind write approval to the exact owner, task, account/resource revision,
  target and payload. Recheck scope and terms at execution. Invalidate a review
  when relevant details change; never replay an uncertain external write.
- Browser access and MCP installation are not blanket permission to submit,
  purchase, send or delete. Preserve host, network, workspace, scope and human
  review boundaries. Do not remove Chat's repository read-only mode to expose
  unrelated connected-service tools.
- Keep tenant secrets, browser profiles, transcripts and health data isolated.
  Use safe connection views and redacted diagnostics. Follow [SECURITY.md](SECURITY.md).
- Migrate existing integrations incrementally with compatibility and rollback.
  Verify with unrelated accounts/sites and multiple owners; distinguish source
  checks, controlled tests and live external success in reports.
