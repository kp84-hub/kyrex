# Apps via Composio (read-only beta)

This optional Cloud connector adds Notion, Slack and Google Drive sign-in to
Connections. The existing Remote MCP registry, tool selection, host mediation,
Bot restrictions and live grant rechecks remain authoritative. Existing direct
Calendar, mail, GitHub, Messages and health connections are not migrated or used
as a fallback. This does not add setup controls to the TUI/IDE.

## Deployment

The Cloud Dockerfile installs the SDK pinned in
`kyrex-cloud/composio-requirements.txt`. For a manual installation, install that
file alongside the normal Cloud dependencies. Python 3.11 or newer is required.

Set `COMPOSIO_API_KEY` in the backend's secret configuration to a dedicated
Composio **project** key. Enable Session management, Session tool execution and
the connected-account reads required to verify account ownership/status.
Never put this key in the frontend or a user prompt. Do not configure an
organization/user API key or `COMPOSIO_CUSTOM_HEADERS` alongside it; the adapter
rejects those ambient credential paths. The SDK API origin is fixed to
`https://backend.composio.dev`, tracking is disabled, and retries are disabled.

Use a separate Composio project for each deployment. Owner IDs are opaque stable
hashes derived on the server from the authenticated Kyrex identity. Existing
connector encryption (`WEB_SESSION_SECRET` or `KYREX_PROVIDER_SECRETS_KEY`) and
persistent `KYREX_DATA_DIR` are required. All pending/ready records use the existing
encrypted `remote_mcp.sqlite3` registry, not a new credential store or task queue.

Without a project key, the catalog explains server setup and disables sign-in.
A particular toolkit may require a custom OAuth app or additional Composio
configuration. Review current Composio plans/managed-auth limits before rollout.

## User flow

1. Open Connections → Apps via Composio → Connect the app.
2. Open Continue app sign-in. Composio hosts consent and manages provider tokens.
3. Return to Kyrex and choose I finished signing in. This verifies the exact
   server-stored account ID, app and owner against Composio; callback query
   parameters never grant access.
4. Expand Remote MCP, select available read tools on the connected app and save.
5. Ask an eligible Bot to use the connected app through `mcp_read`.

Pending setup expires after 15 minutes. It can be checked or cancelled after a
reload; the hosted sign-in link itself is not persisted in browser storage.
If the original sign-in link was lost, cancel the pending setup and start again.

Composio holds the provider authorization. Its consent screen can request scopes
broader than this beta exposes; Kyrex limits execution to explicitly selected,
server-declared read-only/non-destructive tools. Read annotations are claims,
not a sandbox around the third-party server. No tools are selected automatically.

## Enforcement and disconnect

The authentication session has an explicit empty app-tool allowlist. The ready
session is limited to one app and the exact verified connected-account ID.
It uses the direct-tools preset, only read tags, and explicitly disables writes,
sandbox, proxy, in-chat connection management, multi-account execution and
Instant access. Other-app and meta tools are excluded even if marked read-only.
Unsupported transport/schema/tool-count limits remain those of Remote MCP.

Before discovery and immediately before execution, the host verifies the live
account is ACTIVE and still belongs to the stored user/app. It reloads the
broker session and checks its owner, selected account and MCP destination.
The MCP transport still re-lists tools and checks metadata fingerprints,
schemas, public HTTPS/DNS pinning, no redirects and local grant/Bot revocation.

Disconnect deletes the local grant first, so a broker outage cannot retain
Kyrex access, then attempts to delete the hosted session. It does not delete
the provider account, which another authorized session might use. Revoke that
authorization in Composio or the provider settings as well when desired.
An in-flight completion uses the registry's revision check and cannot recreate
a deleted grant. A call already accepted by the remote service cannot be recalled.
A failed setup may leave a non-executable broker session/account; operators can
remove abandoned sessions in Composio.

## Validation and limits

Controlled tests cover two owners, spoofed identity, app/account mismatch,
expired/inactive authorization, pending reload/cancel, missing encryption,
untrusted origins, empty initial grants, write/meta/other-app exclusion,
live account/session changes, disconnect races and safe projections. They use
a fake broker and MCP provider with real SQLite/Fernet/FastAPI. An offline
contract test imports the pinned real SDK and checks the used call signatures.
UI tests render the component with mocked HTTP. Existing MCP mediation tests
remain in CI; no live OAuth/provider credentials are used there.

Local execution was unavailable during implementation; validation is performed
by the PR's GitHub Actions checks. Live Composio sign-in, actual provider tool
annotations/schemas and a deployed Bot read must still be smoke-tested before
calling this beta externally verified. Toolkits without eligible read tools or
within unsupported transport/count limits report no usable tools rather than
relaxing the host gate. Writes, arbitrary catalog apps, Composio webhooks and
MCP OAuth discovery are outside this slice.

Primary references:
- https://docs.composio.dev/docs/sessions-via-mcp
- https://docs.composio.dev/docs/configuring-sessions
- https://docs.composio.dev/docs/authentication/manually-authenticating
