# Connector and service universality audit

Audited 2026-10-15 against `main` commit
`a359780fa582931a6edec89f6a88ee70a07afae2`.
This is a source and architecture audit. It does not inspect production secrets,
prove current connection health, perform external transactions or certify tenant
isolation. The [target architecture](universal-agent-platform.md) describes the
proposed contracts; none are enabled by this documentation change.

## Findings

Kyrex has a reusable engine, policy layer, durable task store, provider adapters
and owner-scoped connectors. The largest obstacle is inconsistent integration:
specialized Chat routes, feature-specific stores and approval paths, multiple
connection catalogs, and deployment-wide assumptions surround that shared core.
A new engine or a separate function for every user request would add to the gap.

Six entries are registered in Chat's connector catalog. Thirteen entries are in
the engine MCP catalog. These are separate inventories, not nineteen verified
live connections. Dedicated inboxes, speech, Browser Hosts, repository publishing,
memory and background services also exist outside the six-card registry.

| Priority | Source finding | Consequence | Required change |
| --- | --- | --- | --- |
| P0: before opening multi-user service | Web and desktop OAuth compare login against one `WEB_ALLOWED_GITHUB_USERNAME`; Telegram accepts one configured chat; automation gateway selects one deployment owner | Owner-scoped storage does not by itself make admission and execution multi-user | Authenticated account admission, per-user transport bindings and automation service grants; retain existing restrictions until implemented |
| P0: before broadening external writes | Review/claim logic is spread among calendar executors, email drafts, Messages queue and booking workflows | Similar safeguards exist but have different lifecycle/recovery semantics | Reuse their exact-payload protections in a shared review/receipt contract; migrate with parity tests, not a global auto-approval switch |
| P0: before general browser effects | Generic click is tier zero; site-specific booking drivers enforce additional network/submit protections | A click classification alone cannot distinguish reading from buying or sending | Effect-aware browser execution and submission boundaries before enabling arbitrary commerce or message sends |
| P1: capability consistency | Static frontend catalog, role-shaped declarations, Bot tool masks and runtime routing shims describe overlapping capabilities | UI, routing and actual authorization can disagree | Backend capability projection shared by discovery, routing, tool exposure and diagnostics |
| P1: task reliability | CloudTaskStore/Flux are durable, but ordinary engine turns and some feature producers follow other lifecycles | Disconnect/restart and status behavior depends on the task type | Shared task IDs, event cursors, checkpoints, leases and unknown-outcome handling |
| P1: user portability | Fixed Level 6 studio, Eastern timezone, 8:30 slot and group destination; operator repository defaults | A second studio/user needs code or deployment changes | Optional workflow configuration with validated account/resource bindings |
| P1: resource portability | Browser profiles are owner-plus-Bot; manual/automation locking already exists | Another Bot may lack the same signed-in session; healthy container can still have unusable Chrome/profile | Explicit owner-owned profile resources, deliberate sharing, layered health and login handoff |
| P2: scale and maintainability | Several JSON stores use process-local locks; desktop tokens/handoffs and some live producers are process-local; SQLite relies on one persistent deployment volume | Adding replicas is not a safe configuration-only change | Transactional registries/auth state and documented storage topology before horizontal scaling |

P0 is a prerequisite for the stated expansion, not a claim that every current
restricted workflow is exploitable. P1 is the first platform migration work;
P2 follows the selected deployment model.

## Inventory method and boundaries

Reviewed the Chat connector registry and safe views; registered backend routers;
Cloud executors, worker and service modules; engine tools, providers, extensions,
MCP manifest/runtime; deployment entrypoints; and device companion documentation
and interfaces. Source links below anchor the audit at integration boundaries.
This is not a line-by-line security or third-party dependency audit.

Catalog sources: [Chat registry](../../kyrex-chat/src/lib/connectorRegistry.js),
[connection APIs](../../kyrex-cloud/web/backend/connections_api.py),
[backend bootstrap](../../kyrex-cloud/web/backend/main.py),
[engine MCP catalog](../../kyrex_engine/kyrex/assets/mcp-connectors.json),
[executor registry](../../kyrex-cloud/serve.py),
[worker startup](../../kyrex-cloud/worker.py).

"Implemented" below means code or a documented companion exists at this
baseline. It does not mean installed, connected, live-tested or universal.

## Account connectors and external data

| Integration and evidence | Current boundary | Universality gap | Migration |
| --- | --- | --- | --- |
| Google Calendar: [store/API](../../kyrex-cloud/connectors.py), [read executor](../../kyrex-cloud/cal_executor.py), [writer](../../kyrex-cloud/cal_writer.py), [editor](../../kyrex-cloud/cal_editor.py) | Owner OAuth with separate read/event-write scopes; account/calendar selection API; read, create, conditional update and reviewed delete paths | Legacy role declarations, prefixes and compatibility wrappers; writer core fixes primary calendar, Eastern timezone and a narrow event field set | Calendar adapter with explicit account/calendar resources and operation-specific grants; user timezone and typed event fields; keep current write previews and conditional checks |
| Standalone legacy Calendar: [executor](../../kyrex-cloud/cal_executor.py), [windows](../../kyrex-cloud/calendar_windows.py) | Non-Chat standalone reader uses deployment Google refresh-token variables, primary calendar and Eastern day windows; owner-facing Chat uses the connector store instead | An operator credential is not a tenant credential; the standalone path cannot be reused as a user connector without changing its identity contract | Require explicit operator context for legacy use; migrate user jobs to owner connection/resource refs and configured timezone, with no environment-token fallback |
| Gmail: [connector](../../kyrex-cloud/connectors.py), [mail bridge](../../kyrex-cloud/web/backend/mail_routing_bridge.py), [continuation](../../kyrex-cloud/web/backend/gmail_continuation_bridge.py), [inline results](../../kyrex-cloud/web/backend/gmail_inline_result_bridge.py) | Separate `gmail.readonly` grant; bounded search/read, MIME/raw-body handling and paging; no Gmail send/modify/delete connector | Multiple chat routing/result continuation layers; frontend copy still describes header-only reads while backend supports bodies | Common `mail.search`/`mail.read` operations and result references; accurately project supported content; retain read-only provider boundary |
| AgentMail forwarding inbox: [store](../../kyrex-cloud/agent_inbox.py), [settings API](../../kyrex-cloud/web/backend/email_inbox_api.py) | Encrypted owner inbox/key; verifies selected inbox and read permissions; read-only; selected source has revision | Separate Connections/settings model; one active email source per owner rather than explicit task account binding | Dedicated-mail adapter and source/account reference; source switch invalidates dependent pages/reviews; no silent personal-Gmail fallback |
| Zoho inbox: [IMAP adapter](../../kyrex-cloud/zoho_inbox.py), [shared inbox store](../../kyrex-cloud/agent_inbox.py) | Dedicated mailbox and app password; TLS, bounded read-only INBOX; two supported US IMAP hosts; no IMAP mutations | Provider-specific setup and source selector outside main catalog; regional support is deliberately limited | Keep IMAP mechanics in provider adapter; add catalog status, mailbox identity and supported-region metadata; expand regions only through validated support |
| Zoho sending: [OAuth/HTTPS](../../kyrex-cloud/zoho_send.py), [draft store](../../kyrex-cloud/email_send.py), [Chat](../../kyrex-cloud/web/backend/email_chat.py), [API](../../kyrex-cloud/web/backend/zoho_send_api.py) | Separate owner OAuth tied to dedicated inbox revision; exact From/To/subject/body review, single claimed send; unresolved results do not auto-retry; US endpoints | OAuth app setup is server configuration, while sender grant is user configuration; draft approval is its own task lifecycle | `mail.prepare_send` and reviewed `mail.send` through common task/effect contracts; preserve mailbox binding, recipient resolution and unknown-send handling |
| Messages phone snapshots: [store](../../kyrex-cloud/device_messages.py), [companion](../../kyrex-messages-pairing-android/README.md), [API](../../kyrex-cloud/web/backend/messages_api.py) | User pairing and revocable upload credential; encrypted bounded recent-text snapshot; cloud does not receive Google pairing cookies; reads have sync time | Snapshot freshness/coverage and device state differ from live mailbox semantics; historical doc sections describe earlier read-only versions | Message adapter declares snapshot/live provenance, coverage and timestamp; pair devices as account resources, preserve per-device consent |
| Messages phone sends: [queue](../../kyrex-cloud/messages_send.py), [phone protocol](../../kyrex-cloud/messages_phone.py), [API](../../kyrex-cloud/web/backend/messages_api.py) | Separate phone send consent; existing-thread resolution, exact review and single claimed delivery attempt; unknown outcome is not requeued | Parallel send states/expiry/acks; phone readiness and review are coupled to special Chat handling | Common send preparation/review/receipt, with phone execution adapter; retain SIM/participant checks and distinguish provider acceptance from recipient delivery |
| Google Messages browser reads: [consent/RPC](../../kyrex-cloud/web_messages.py), [host connector](../../browser-host/messages_connector.py), [watcher](../../browser-host/messages_watcher.py) | Owner-selected live enrolled host; browser pairing cookies stay there; read-only connector separate from phone snapshots | Same UI family covers different resources and freshness; host selection can be ambiguous | Explicit provider mode, account/host binding and separate capabilities; never silently switch phone/browser data or send paths |
| GitHub reads: [connection](../../kyrex-cloud/github_connection.py), [App flow](../../kyrex-cloud/github_app.py) | Encrypted owner PAT/App connection and selected repositories; bounded file/directory reads | Legacy PAT and App connection modes, role-shaped safe view, separate publishing connection | Repository read adapter and explicit repository/ref resource; retain selected-repo enforcement and separate publish authority |
| GitHub developer work/publishing: [workspaces](../../kyrex-cloud/developer_repositories.py), [git workflow](../../kyrex-cloud/git_workflow.py), [developer host](../../kyrex-cloud/web/backend/dev_bot.py) | Owner repository workspaces; separate reader/publisher App credentials; reviewed changes and branch/PR publication; legacy fresh-clone workflow also exists | Global target-repo default in older paths; code workflow, ordinary Chat and connected reads use different orchestration | Common workspace/repository resource plus typed edit/publish effects; preserve review, isolation and selected-repo boundaries; no automatic merge |
| Oura Ring: [fitness store](../../kyrex-cloud/fitness_connections.py), [setup](../connectors/fitness.md) | Owner OAuth, refresh and bounded read-only sleep/readiness/activity/heart-rate/workout data | Fitness policy and report/delegation paths remain separate from shared calendar/mail access | Fitness adapter with dataset scopes, dates/timezone, provenance and freshness; keep explicit health restrictions; normalize data without inventing missing metrics |
| Samsung Health / Health Connect: [companion](../../kyrex-health-android/README.md), [fitness store](../../kyrex-cloud/fitness_connections.py) | User-paired Android uploads; chosen read permissions; bounded records; optional WorkManager background sync; skipped-record reporting | Phone permissions, server pairing, background eligibility and data completeness are separate readiness states | Device ingestion adapter under fitness capability; show last sync, coverage and permission state independently; preserve upload consent and revocation |
| Glofox schedule reads: [API](../../kyrex-cloud/glofox_api.py), [class reader](../../kyrex-cloud/workout_class_reader.py) | Read-only public guest bootstrap; pinned branch/timezone/class/8:30 slot and strict schema checks | Fixed studio and slot are product assumptions, not a generic fitness connector | Configured studio adapter with validated branch identity, timezone, class and schedule query; no arbitrary URL/credential injection |
| Glofox member booking: [schema](../../kyrex-cloud/workout_booking_schema.py), [host driver](../../browser-host/workout_booking_driver.py), [Chat](../../kyrex-cloud/web/backend/workout_booking_chat.py), [guide](../glofox-bookings.md) | Member login in owner/Bot profile; exact class review; single submission; current supported path excludes paid/drop-in/waitlist and bulk bookings | Fixed studio; specialized routing/confirmation/state and disconnect producer; working schedule read does not establish member login or completed reservation | Optional studio booking adapter on common booking/task/review/resource contracts; preserve all unsupported-case blocks and member-account verification |
| Restaurant browser reservations: [schema](../../kyrex-cloud/booking_schema.py), [store](../../kyrex-cloud/booking_store.py), [driver](../../browser-host/booking_driver.py), [Chat](../../kyrex-cloud/web/backend/booking_chat.py) | Structured preparation, missing-details questions, observed browser review and exact confirmation; controlled form-driver support | Reservation workflow has its own preparation/recovery path; no guarantee across all booking sites | General reservation capability with website adapter where needed; share task lifecycle/effect guard with other booking types; retain changed-details invalidation |

Account selection needs special care: several stores key one connection by owner
and provider/source. Supporting multiple Google accounts or mailboxes requires
schema and identity migration, not merely adding a dropdown. Pending work must
stay bound to the selected account and connection revision.

## Browser, execution and extensibility

| Service and evidence | Reusable foundation | Gap and required change |
| --- | --- | --- |
| Browser Host: [agent](../../browser-host/agent.py), [enrollment](../../kyrex-cloud/browser_hosts.py), [channel](../../kyrex-cloud/browser_host_channel.py), [dispatch bridge](../../kyrex-cloud/browser_host_bridge.py) | Authenticated owner-bound host; WebSocket heartbeat, worker-to-web durable dispatch, bounded jobs | Current host enrollment belongs to one owner: support multiple enrolled resources or deliberately isolated hosted sessions, never simply remove owner checks. Distinguish channel loss, unavailable resource, busy lease and browser launch error; version capabilities and persist task/attempt references across reconnects |
| Browser operator: [executor](../../kyrex-cloud/browser_operator.py) | Structured navigate/read/click/type/upload/download/screenshot/delete; policy handshake, per-Bot site allowlists, workspace limits | Generic primitives coexist with fixed-purpose actions. General adaptation needs an observe/act loop plus effect classification and network submission guards; do not remove site restrictions globally |
| Browser sessions and manual viewer: [sessions](../../kyrex-cloud/browser_sessions.py), [profiles](../../browser-host/profiles.py), [locking](../../browser-host/manual_mode.py), [viewer control](../../browser-host/viewer_ctl.py) | Persistent owner/Bot profiles; exclusive automation/manual access; bounded private viewer | Profile identity should follow selected owner/account/workspace; secure human handoff must be visible on every surface. Container health is not browser/session health. Profile migration requires opt-in, no cookie merging |
| Filesystem execution: [executor](../../kyrex-cloud/fs_executor.py), [engine toolbox](../../kyrex_engine/kyrex/toolbox.py) | Workspace-confined Cloud operations and local editor/command tools with gates | Local and Cloud trust boundaries differ. Model/tools cannot select arbitrary host paths; register workspace resources and keep explicit local execution trust |
| Python extensions: [registry](../../kyrex_engine/kyrex/extensions/registry.py), [engine dispatch](../../kyrex_engine/kyrex/core.py) | Schemas and callable registration, local discovery; engine tool allowlist | Decorators do not declare a full effect/scope/recovery contract; import runs local Python. Require approved installation and execution mediation/isolation before offering hosted plugins to tenants |
| MCP runtime/catalog: [runtime](../../kyrex_engine/kyrex/tools/mcp.py), [catalog management](../../kyrex_engine/kyrex/mcp_connectors.py) | Configured subprocess tools, schema discovery, install/test/remove lifecycle | Manifest verification is not a live account check. Subprocess inherits process environment; local config and paths are not tenant-scoped authority. Use owner-scoped isolated runners, restricted environment and mediated tools before hosted use |

The engine MCP catalog is a distribution/setup catalog, not a substitute for the
Chat account connector registry. Preserve both uses through one descriptive
inventory with different installation and runtime states.

All thirteen current MCP entries are accounted for below. Package names and
verification labels are recorded from the pinned manifest, not revalidated
against current vendor releases in this audit.

| MCP ID | Declared purpose/setup | Universal-platform treatment |
| --- | --- | --- |
| `filesystem` | Allowed directory, Node server | Workspace resource; read/write effects distinct; no tenant host filesystem |
| `cloudflare` | API token/account ID; cloud resources | Account/resource scopes; DNS/deploy/delete effects need policy/review |
| `everything` | Reference/test server | Development/test-only registration; not a production user service |
| `fetch` | URL fetching, uvx | Network policy, response bounds and untrusted content handling |
| `git` | Local repository, uvx | Workspace binding; read and repository mutation effects separated |
| `memory` | Local knowledge graph file, Node | Tenant/workspace-specific storage and explicit retention; do not reuse one home-directory file across users |
| `sequential-thinking` | Reasoning utility, Node | Bounded optional engine utility; no service/account privilege implied |
| `time` | Timezone utility, uvx | Validated timezone service; user/studio timezone comes from configuration |
| `github` | Manual launch/auth setup required by current manifest | Distinguish catalog entry from connected GitHub App; explicit selected repositories and credentials |
| `playwright` | Browser server and browser binaries | Enrolled isolated browser resource; cannot bypass review/manual profile lease |
| `context7` | Documentation lookup; optional configuration-dependent auth | Read-only external lookup, provenance and bounded network use |
| `notion` | Integration token and workspace/page access | New provider adapter or mediated MCP capability; no claim of an existing Chat Notion connection |
| `sentry` | Manifest requires manually replacing CLI token placeholder | Keep blocked until valid setup; avoid exposing credentials in command lines in a hosted adapter; no false environment-auth claim |

## Model and decision services

| Service and evidence | Current behavior | Migration |
| --- | --- | --- |
| LLM profiles: [encrypted profiles](../../kyrex-cloud/web/backend/provider_profiles.py), [Bot resolver](../../kyrex-cloud/web/backend/bot_provider.py), [onboarding](../../kyrex-cloud/web/backend/chat_onboarding.py) | Per-owner saved profiles; Bot stores profile reference and exact model; no silent global fallback for configured Bot turns | Common provider catalog and structured capability/health checks across surfaces; distinguish authentication, model discovery, inference and tool support |
| Engine providers: [factory](../../kyrex_engine/kyrex/providers/__init__.py), [OpenAI-compatible](../../kyrex_engine/kyrex/providers/openai_.py), [Anthropic](../../kyrex_engine/kyrex/providers/anthropic.py), [configuration](../../kyrex_engine/kyrex/config.py) | Native Anthropic and OpenAI-compatible paths; local endpoints supported through configuration; separate local/env and hosted profile setup | Protocol adapters behind one provider interface; local endpoints remain execution-location-specific, not arbitrary hosted internal-network access |
| OpenCode Go: [protocol selection](../../kyrex_engine/kyrex/opencode_go.py), [Google](../../kyrex_engine/kyrex/providers/opencode_gemini.py), [Messages](../../kyrex_engine/kyrex/providers/opencode_messages.py), [check](../../kyrex-cloud/web/backend/bot_model_check.py) | Model-specific Responses, Messages, Google and chat-completions dispatch behind one profile; legacy Go base normalization | Keep endpoint/protocol differences inside adapter, not duplicate profiles per model family. A listed model can still reject inference. Gemini live HTTP 400 remains unresolved at this baseline; do not call it fixed |
| Jev/TypeSafe routing: [Chat client](../../kyrex-cloud/web/backend/jev_routing.py), [router](../../kyrex-cloud/web/backend/jev_stream_router.py), [gateway](../../automation/jev_router.py), [engine decisions](../../kyrex_engine/kyrex/decision.py) | Optional decision service/gateway; constrained routing and deterministic fallback; execution policy stays on host | Decision requests consume safe capability projection. Transport-selected credentials and legal routes remain host-controlled; router cannot grant tools or approve effects |
| Legacy intent/repo model calls: [intent](../../kyrex-cloud/intent.py), [git workflow](../../kyrex-cloud/git_workflow.py), [headless agent](../../kyrex-cloud/headless_agent.py) | Older paths can use deployment environment/model configuration | Explicit execution-context provider resolution for user jobs; keep an intentional operator/service identity for infrastructure decisions, with separate accounting |

Changing model should not require rewriting a connector or adding another
website route. It can require a protocol adapter and verified model support.
Do not silently consolidate or remove user profiles during this migration.

## Voice, surfaces, identity and storage

| Component and evidence | Current behavior | Universal-platform work |
| --- | --- | --- |
| Voice settings/providers: [speech store](../../kyrex-cloud/voice_speech.py), [provider calls](../../kyrex-cloud/voice_speech_providers.py), [API](../../kyrex-cloud/web/backend/voice_api.py) | Owner-encrypted Google Cloud/OpenAI reply speech and Groq/OpenAI PC transcription; device reply option; Android uses phone recognition | Keep speech credentials separate from LLM/cloud memory credentials. Publish per-operation availability and supported languages/voices; current defaults/voice lists focus on English-US |
| Voice devices: [leases](../../kyrex-cloud/voice_devices.py), [browser/native guide](../../kyrex-voice/README.md), [Android](../../kyrex-voice-android/README.md), [Windows](../../kyrex-voice/windows/companion.py) | Per-device revocable credentials, listening leases, shared ordinary Chat conversation; browser requires visibility; native companions handle background listening | Retain OS-specific capture, one active listener and duplicate-turn controls. Link submitted voice turns to common task IDs; configurable destination rather than a separate execution stack |
| Chat/UI/onboarding: [service](../../kyrex-cloud/web/backend/chat_service.py), [API](../../kyrex-cloud/web/backend/chat_api.py), [onboarding](../../kyrex-cloud/web/backend/chat_onboarding.py), [Connections UI](../../kyrex-chat/src/components/ConnectionsSettings.jsx) | Owner/Bot engine sessions plus many early specialized routes and feature cards; deterministic model setup | Surface submits intent and displays common task/review/status data. Generate supported capabilities from backend definitions; retain useful service-specific setup UI without duplicating execution rules |
| TUI/IDE/VS Code/headless: [bridge](../../kyrex_engine/core_bridge.py), [TUI](../../tui), [IDE](../../kyrex-ide), [VS Code](../../kyrex-vscode/README.md), [headless](../../kyrex-cloud/headless_agent.py) | Shared engine protocol, separate setup/transport/trust contexts | Explicit local/cloud identity linkage and capability adapters. Same contracts do not require moving local files or secrets to Cloud |
| Web/desktop auth: [main](../../kyrex-cloud/web/backend/main.py), [browser sessions](../../kyrex-cloud/web/backend/web_sessions.py) | GitHub OAuth, one allowed username, durable hashed browser-session credentials; process-local desktop handoff/access/refresh token maps | Replace single-user admission deliberately; persistent revocable device sessions and auth transactions before replicas; enforce owner identity server-side on every lookup/action |
| Telegram: [transport](../../kyrex-cloud/telegram_bot.py), [worker integration](../../kyrex-cloud/worker.py) | One configured chat and default/aliased repositories; ephemeral chat context; worker integration exists | Per-user verified chat/account bindings; same task submission/review protocol; do not remove allow-chat restriction before replacement |
| Explicit Chat memory: [Firestore adapter](../../kyrex-cloud/web/backend/chat_memory.py), [guide](../../kyrex-cloud/web/backend/CHAT_MEMORY.md) | Opt-in remembered facts scoped by owner; deployment service account; transcripts remain separate | Storage interface with owner namespaces, quotas, deletion/export and provenance. Keep explicit memory separate from transcript retention and provider context |
| Engine knowledge/memory: [toolbox](../../kyrex_engine/kyrex/toolbox.py) | Bounded local Markdown lookup in project memory/docs; separate from Chat Firestore and MCP memory | Distinct memory kinds with explicit owner/workspace scope and retrieval semantics; do not imply all three memory stores are synchronized |
| Bot/routing identity: [registry](../../kyrex-cloud/bots.py), [policy projection](../../kyrex-cloud/web/backend/bot_capabilities.py), [shared tools](../../kyrex-cloud/web/backend/shared_connected_tools.py), [routing identity](../../kyrex-cloud/web/backend/routing_identity.py), [worker overlay](../../kyrex-cloud/connected_tool_context.py) | Owner/lifecycle policies; shared connected Calendar/mail routing shims exist; fitness remains separately policy-gated; monkeypatch compatibility installation | Use typed context and one capability resolver. Preserve explicit denies and coordinator permission; persona names are specialization only. Remove wrappers only after behavioral parity |
| Task/event/approval storage: [task store](../../kyrex-cloud/task_store.py), [Flux](../../kyrex-cloud/flux.py), [delegation](../../kyrex-cloud/delegation.py), [contracts](../../kyrex-cloud/job_contracts.py) | Durable task claims/events/replies, browser dispatch and child jobs; validated specialized job contracts | Extend existing store with common preparation/review/effect references, reconnect semantics and recovery. Contracts/plans remain descriptive, never permission grants |
| Research/result delivery: [completion outbox](../../kyrex-cloud/web/backend/research_completion.py), [browser follow-up](../../kyrex-cloud/web/backend/overwatcher_workflow.py), [developer updates](../../kyrex-cloud/developer_updates.py) | Durable outbox summarizes existing browser evidence with tools disabled; bounded coordinator observation and safe developer progress projections | Retain these useful patterns under common task result delivery and provenance. A summary worker must not restart tools or treat an observed result as a new action grant |
| Other persistence/audit: [paths](../../kyrex-cloud/paths.py), [audit](../../kyrex-cloud/audit.py), [engine audit](../../kyrex_engine/kyrex/audit/reasoning.py), [privacy](../../kyrex-cloud/web/backend/chat_privacy.py) | SQLite, owner JSON files, workspace artifacts and logs have different ownership/retention schemes | Inventory retention and deletion by store; redact tool arguments/results before audit persistence; transaction-safe multi-process updates; no assumed global delete/export |
| Workspace cleanup: [Cloud cleanup](../../kyrex-cloud/rift_cleanup.py), [local Rift implementation](../../internal/rift) | Explicit guarded Cloud Rift deletion with retention, active-work/lease checks and persistent markers; separate local workspace clones | Common resource lifecycle metadata and owner/operator deletion authority; preserve guarded cleanup and distinguish deleting workspaces from revoking accounts or deleting provider data |

## Automations, domain workflows and deployment

| Service and evidence | Current behavior | Universal-platform work |
| --- | --- | --- |
| Email watcher/rules: [watcher](../../automation/email_watcher.py), [API](../../kyrex-cloud/web/backend/automation_api.py), [rules](../../kyrex-cloud/automation_rules.py), [event handling](../../kyrex-cloud/email_event.py), [deployment](../../automation/README.md) | Owner-scoped rules and deduped Gmail reads; VPS polls with deployment-configured owner credential; explicit sender/Bot/conversation; bounded lookback/baseline | Per-owner service grants and source subscriptions; shared trigger-to-task envelope. Keep sender routing separate from trust and preserve baseline/dedup receipts; no automatic sends from read consent |
| Daily calendar report: [scheduler](../../kyrex-cloud/scheduler.py), [Telegram](../../kyrex-cloud/telegram_bot.py) | Report hour/timezone from deployment env, transport-injected delivery and existing executor | User schedule, timezone, selected account and destination; durable next-run/misfire policy; same task result delivery |
| Level 6 weekly/calendar/batch: [weekly](../../kyrex-cloud/level6_weekly.py), [calendar](../../kyrex-cloud/level6_calendar.py), [batch](../../kyrex-cloud/level6_calendar_batch.py), [Facebook reader](../../browser-host/level6_post.py) | Fixed Facebook page/post marker, studio schedule and Eastern week logic; preview/review paths | Optional studio workflow configuration and source adapters; common schedule extraction, calendar preparation and review. Keep source freshness and mismatch checks |
| Level 6 scheduled messaging: [schedule](../../kyrex-cloud/level6_message_schedule.py), [serve gates](../../kyrex-cloud/serve.py), [browser actions](../../kyrex-cloud/browser_operator.py) | Opt-in preview schedule; fixed-purpose Google Messages operation and default group label; sending has separate enable/review gates | Configured schedule/recipient and reviewed messaging adapter. Do not generalize fixed-destination tier-zero operation into arbitrary sends |
| Trainer monitor: [monitor](../../kyrex-cloud/level6_trainer_monitor.py), [store](../../kyrex-cloud/level6_trainer_store.py), [API](../../kyrex-cloud/web/backend/level6_trainer_api.py) | Owner settings, persistent observations/outbox/dedup and leases; domain-specific schedule comparisons and delivery | Reusable monitored-resource/change-event and outbox pattern; retain trainer-specific comparison as a workflow adapter, with explicit delivery authority |
| Fitness reports/profile: [workout](../../kyrex-cloud/workout_report.py), [sleep](../../kyrex-cloud/sleep_report.py), [profile](../../kyrex-cloud/web/backend/fitness_profile.py), [delegation](../../kyrex-cloud/fitness_delegation.py) | Connected fitness data, user preferences, structured reports and delegated read jobs | Shared data/result provenance and account/timezone context; report templates stay modular; editing preferences must not imply wearable write access |
| Workout follow-up: [module](../../kyrex-cloud/workout_message_followup.py) | Domain-specific continuation between workout context and messaging | Generic task/result references and recipient-bound draft preparation; prior task content cannot authorize a new send |
| Web/worker supervision: [entrypoint](../../kyrex-cloud/web/entrypoint.py), [worker](../../kyrex-cloud/worker.py), [web Dockerfile](../../kyrex-cloud/web/Dockerfile) | One web plus one worker sharing persistent storage; supervisor tears down both on required child failure | Keep deployment topology explicit; worker recovery/resource reconnection are shared service concerns; per-tenant quotas/fairness before broader admission |
| VPS/browser/viewer containers: [cloud compose](../../browser-host/docker-compose.cloud.yml), [viewer compose](../../browser-host/docker-compose.viewer.yml), [sandbox guide](../../browser-host/RAILWAY_SANDBOX.md) | Separately enrolled browser resources with persistent profiles and optional viewer; cloud/sandbox modes | Deployment templates with no personal hostname/owner assumptions, versioned enrollment and layered health; preserve private viewer transport |
| Decision sidecar/tunnel and watcher deployment: [Jev compose](../../automation/docker-compose.jev.yml), [watcher compose](../../automation/docker-compose.yml), [guide](../../automation/README.md) | Separate service credentials; Jev loopback service exposed via authenticated tunnel; watcher has no Google/LLM secrets | Infrastructure transport, not an account connector. Least-privilege service identity and observable health; tunnel reachability does not prove downstream capability health |
| Build/static distribution: [site](../../site), [scripts](../../scripts), [voice assets](../../kyrex-voice/build_web_wake.py) | Website/assets, CI packaging and companion/wake-model distribution | Track release/schema compatibility separately from connected status. Artifact download/build hosts are supply-chain dependencies, not user-granted runtime services |

No native generic shopping/payment connector or universal checkout executor was
found in the reviewed runtime/catalogs. Notion, Cloudflare and Sentry catalog
entries likewise do not establish hosted Chat account connections. Broader
services should enter through the same capability/adapter contracts, with honest
unsupported status until implemented and verified.

## Android app consolidation follow-up

The [Android-to-Chat feasibility audit](android-chat-integration.md) reviews all
three Android application projects, their manifests, native services, credential
storage and existing Chat setup paths. It replaces the assumption that each
phone feature should require a separate companion: use Chat directly for web
capabilities and offer one optional Kyrex Chat Android app for phone-only APIs.
Health Connect and dependable screen-off voice cannot be moved into ordinary
website JavaScript. Messages has an existing browser-host read path, but its
new-connection entry point currently starts phone pairing; it needs deliberate
integration and live validation before becoming a download-free alternative.

## Cross-cutting changes to make once

1. **One safe catalog projection.** Return service, account/resource reference,
   operation, grant/readiness state and actionable setup requirements. Separate
   configured, authenticated, authorized, resource-online, busy, stale-data and
   unsupported. The current `/api/connections` top-level read-only flag and some
   legacy comments/copy do not describe all separate write surfaces; do not use
   them as permission truth.
2. **One execution context.** Derive identity and selected resources on the host,
   pass them through engine, worker, delegates and devices. Provider fallback,
   Bot names and model routing cannot change those bindings.
3. **One task/effect lifecycle.** Reuse durable tasks/events; bridge current
   feature reviews and receipts. Ordinary reads, preparation, approval waits,
   external attempts and reconciliation have distinct states. Screen-off is a
   subscriber disconnect; it is not cancellation or permission to resubmit.
4. **One resource handoff model.** Browser, phone and workspace adapters expose
   leases, readiness and secure human steps. Preserve exclusive access; show the
   actual active task rather than only delegated records.
5. **One trigger contract.** User message, voice, timer and inbound change create
   the same authorized task with an owner-scoped idempotency key. Each automation
   has explicit source, filters, destination, expiry/disable and effect authority.
6. **Configuration over personal constants.** Store validated user/workflow
   preferences and explicit accounts/resources; vendor origin/scopes remain
   code-controlled protocol constraints. Do not turn a branch constant into an
   unrestricted URL parameter.
7. **Shared observability and retention.** Task IDs and redacted error categories
   connect UI, workers and agents; scope/lease/attempt state is inspectable without
   secrets. Define retention/export/deletion per data class and provider. Health
   probes must not silently submit paid inference, mail or booking requests.

## Recommended order and validation

Start with Calendar and email because they exercise the most important connected
account behavior without depending on an interactive browser login. Implement
the backend capability projection and execution context around their current
adapters, then expose combined read tools through the existing engine. Migrate
one reviewed calendar write onto shared task/review semantics next.

Next migrate browser preparation and task recovery, followed by configurable
booking workflows, messaging/automation delivery and device parity. Work on
multi-user admission and storage/resource isolation before admitting additional
tenants. Keep all legacy routes available through compatibility adapters until
their replacement meets the same checks; never run both effect paths.

| Validation area | Evidence required before calling migration complete |
| --- | --- |
| Inventory parity | All six Chat catalog entries, thirteen MCP entries and separate services above have explicit supported/unsupported and setup states |
| Owner isolation | Two owners with different accounts, models, studios/timezones and devices; guessed IDs and delegated/scheduled calls cannot cross boundaries |
| Capability parity | Same allowed operations across eligible Bot personas; explicit denies, live scope revocation and selected-resource restrictions still apply |
| Effect parity | Exact review, changed-details expiry, duplicate confirmation, concurrent approval, provider rejection and uncertain outcome checks |
| Recovery | Screen sleep, client network loss, web/worker/agent restart, stale resource lease and cross-device reattach; no blind external write replay |
| Browser portability | A supported unfamiliar site needs no new Chat route; unsupported site/login/payment returns truthful waiting/blocked state; no effect via a nominally safe click |
| Device/automation parity | Revoked phone/speech credentials, stale snapshots, schedule timezone/DST and duplicate inbound events; no implicit send consent |
| Live evidence | Separate controlled test results from verified provider outcomes; booking success requires provider confirmation, not merely completed automation |

Useful existing suites to extend include shared calendar tools, connectors,
task store/Flux, browser host/manual-mode, email send, Messages send, fitness,
voice devices and workout booking/recovery tests. This audit adds no runtime
behavior and does not claim those suites were rerun or that production health
was checked. Documentation validation checks source links, inventory coverage
and PR scope; implementation phases require the behavioral evidence above.
