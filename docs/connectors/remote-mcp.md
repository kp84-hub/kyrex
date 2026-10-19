# Remote MCP in Kyrex Chat

This first slice connects a public remote MCP server to Bot chats and
workspace-attached Chat through the existing Python engine. Ordinary Chat
without an engine/workspace does not gain a second agent loop.

## Connect

1. Open **Connections → Remote MCP**.
2. Name the connection and enter its public HTTPS MCP endpoint. This slice uses
   port 443 and rejects query parameters, URL credentials and private addresses.
3. Optionally enter a Bearer token or an `x-api-key` credential supplied by the
   server. OAuth discovery/sign-in is not implemented in this slice.
4. Press **Add and test connection**. This performs initialization and paginated
   tool discovery only; it executes no application tool.
5. Select the read tools and press **Save selected tools**. New tools are never
   selected automatically. Saving an empty selection disables all its tools.
6. Ask an eligible Bot to list/use your connected MCP tools.

Only tools explicitly advertising `readOnlyHint: true` and
`destructiveHint: false`, with supported object input schemas, can be selected.
These are claims by the remote server, not isolation of its implementation;
connect only servers you trust. Writes and tools with unknown effects remain
unavailable. A Bot's explicit `mcp:read`, `mcp:*` or `*` denial, raised approval
tier, stopped state or different owner blocks access. Specialized fitness-only
turns retain their existing restricted tool set.

**Test connection** refreshes the tool list. A changed tool's selection is
removed; select it again after review. Execution rechecks the live tool metadata,
input schema, selected connection revision and Bot restrictions before calling.
**Disconnect** removes the encrypted connection and future access. Removing a
connection while a test is in flight cannot recreate it.

## Implementation and boundaries

- Shared transport: `kyrex_engine/kyrex/tools/remote_mcp.py`. Supports negotiated
  MCP 2025-06-18/2025-11-25 Streamable HTTP, JSON and request-scoped SSE replies.
  Newer incompatible protocol revisions report unsupported rather than guessing.
- Chat host adapter: `kyrex-cloud/remote_mcp_connections.py`. Authenticated owner
  identity comes from the host. SQLite transactions store complete encrypted
  records under `KYREX_DATA_DIR/remote_mcp.sqlite3`, using the existing connector
  encryption configuration. No new server secret is needed. Follow the existing
  single persistent-volume deployment model; this is not a cross-host registry.
- `mcp_read` lists selected schemas and invokes a selected read through the
  existing engine/host confirmation channel. It does not grant repository writes
  or shell access. Engine tool auditing/activity remains the existing path.
- Endpoint and key stay server-side after submission. Safe settings return only
  the label, hostname, key-presence flag, tool metadata and test timestamp. URLs
  can carry session capabilities in their paths, so the full URL is encrypted too.
- Every request resolves and pins a public IP, retains the original TLS SNI/Host,
  ignores environment proxies and refuses redirects. Tool calls are not retried.
  Responses, pagination, arguments and execution time are bounded.
- Existing TUI/IDE subprocess MCP configuration continues unchanged. Chat does
  not start the deployment's local MCP processes or permit `/mcp` installation.
  Local servers require a separately authorized host bridge, which is deferred.

Composio's app directory, managed OAuth, write approvals, subscriptions/triggers,
MCP resources/prompts and local-device bridging are not shipped here. The shared
remote transport and host mediation are the foundation for a later optional
broker adapter; no Composio key is collected or used by this change.

## Validation

Controlled tests cover HTTP/SSE protocol messages, DNS/redirect boundaries,
encrypted two-owner settings, tool selection and changed grants, disconnects,
live Bot restrictions, the real engine-to-host-to-transport loop with a scripted
model and mock HTTP server, and rendered connection controls. No live provider
account, paid tool call or deployed connection is implied by those tests.
