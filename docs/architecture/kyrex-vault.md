# Kyrex Vault: source audit and proposed first release

Status: design only, 2026-11-03. Source baseline:
`9ff50978486a20dd4660b1c2ca1742a34f3de6d2` (`main`, after PR #540).
No Vault API, key, credential migration, browser action or permission is enabled
by this document. Production environment, backups and live browser sign-in were
not inspected. This is an implementation plan, not a security certification.

## Product outcome

An owner saves a login through Settings > Vault or a secure form, chooses which
Bots may use it, and authorizes a sign-in to the exact website. The Bot receives
an item reference and a short result, never the password. Existing signed-in
browser profiles remain usable under their current access rules. API keys use
trusted provider/connector adapters and secret references, never model-readable
files, shell environments or generic secret retrieval tools.

The first release should be a complete vertical slice: manage a saved website
login, approve one supported login form, fill it through the existing Browser
Host, and verify sign-in without putting the value into Chat or task history.
Add an existing provider-key reference to the same management surface without
duplicating or moving its credential. Cards, payments, CSV import, contact-book
storage, automatic OTP reading and third-party password-manager integrations
are later work. Existing OAuth connections remain the preferred path when a
supported service provides one.

## What already exists

| Boundary | Source evidence | Reuse / required extension |
| --- | --- | --- |
| Encrypted provider keys with safe public views | [provider_profiles.py](../../kyrex-cloud/web/backend/provider_profiles.py), especially `_box`, `_read`, `public_profile`, `get_profile` | Retain existing provider identity and adapters. Add references rather than copying all keys. Public Vault reads must never decrypt a value for display. |
| Owner-scoped encrypted OAuth tokens | [connectors.py](../../kyrex-cloud/connectors.py), `_box` and `seal_tokens` | Preserve connector grants and resource selection; a saved password does not grant Calendar/mail access. |
| Browser session metadata | [browser_sessions.py](../../kyrex-cloud/browser_sessions.py) | Keep managed session identity separate from a reusable saved login. |
| Live host and Bot policy checks | [browser_host_channel.py](../../kyrex-cloud/browser_host_channel.py), `HostManager.dispatch_browser_task` | Recheck the actual execution Bot, owner, host and profile binding; delegation does not inherit another Bot's grant. |
| Durable browser dispatch | [browser_host_bridge.py](../../kyrex-cloud/browser_host_bridge.py), `request_browser_dispatch`; [task_store.py](../../kyrex-cloud/task_store.py), `submit_browser_dispatch` | Store references, approvals and outcomes only. Never put decrypted credentials into `task_text` or ordinary dispatch records. |
| Browser input and output | [browser_operator.py](../../kyrex-cloud/browser_operator.py), `PlaywrightDriver.type`, `text`, `screenshot`, and generic action execution | Current `type` takes literal action text. A dedicated trusted fill operation is needed; generic typing plus log redaction is insufficient. |
| Exclusive profile access | [manual_mode.py](../../browser-host/manual_mode.py), `acquire` | Use the existing profile lease. Do not race the manual viewer with credential fill. |
| Persistent browser sign-in | [browser_session_cookies.py](../../kyrex-cloud/browser_session_cookies.py); [session recovery guide](../browser-session-recovery.md) | Cookies remain host-side. Removing a Vault item is not browser logout or provider-side revocation. |
| Policy and approval foundations | [policy.py](../../kyrex-cloud/policy.py), [capability projection](../../kyrex-cloud/web/backend/bot_capabilities.py), [platform architecture](universal-agent-platform.md) | Add narrow operation definitions to the existing engine/policy/task lifecycle. Do not build a second agent, queue or permission engine. |

Provider profiles, OAuth tokens and browser metadata currently choose
`WEB_SESSION_SECRET` first, falling back to `KYREX_PROVIDER_SECRETS_KEY`, using
related but separate Fernet derivations. The new Vault must have an independent
keyring and explicit key version; changing the web-login secret must not change
Vault decryption. Do not silently migrate or re-encrypt existing stores as part
of introducing Vault. In particular, a failed decrypt must produce a locked or
unavailable state, not an empty-list claim or an overwrite of unreadable data.

## User experience

Settings > Vault lists a nickname, exact website origin, item type, allowed Bots,
last use and enabled/locked status. Usernames are masked in owner listings by
default; labels and all metadata remain untrusted display data. Passwords and
API-key values are write-only through this UI: replace, disable or remove them,
but never reveal/copy them after saving. No secret preview, export or last-four
fragment is needed for new Vault items.

For a missing website login, Chat may show a secure card naming the exact
website and the requesting Bot. Its fields submit directly to an authenticated
Vault endpoint. They are never a user message, tool argument to the model,
transcript attachment, pending-turn draft, analytics property or browser storage
entry. Clear fields after saving, cancellation and unmount. The owner explicitly
chooses whether to save the credential; temporary use is a later extension.

Before use, show the exact website, selected account and requesting Bot in a
sign-in review. V1 requires a fresh review for each fill; any future standing
grant needs a separate explicit configuration. Logging in authorizes only login,
not a booking, send, purchase, deletion or account-setting change. Existing
external-action reviews remain required.

For MFA, passkeys, CAPTCHA, SSO redirects and unsupported forms, offer the
existing manual takeover. Do not collect these through ordinary Chat or expand
email access to fetch verification codes. Vault cannot promise to bypass a
provider's automation restrictions.

## Storage, keys and trust boundary

Proposed `VaultStore` uses transactional owner-scoped records on the current
shared persistent data volume, with metadata and encrypted payloads separated.
Use the existing cryptography library, not a custom cipher. An independent
random Vault keyring supports explicit active/retired key IDs and rotation;
Fernet/MultiFernet is an available starting point. Seal owner, immutable item ID,
kind and revision with the sensitive payload, and check those bindings when
opening it so ciphertext cannot be transplanted between owners/items.

Key configuration belongs outside the database and repository. Keep an offline
recovery copy separately from ciphertext backups. Use a transactional rotation
procedure with verified decrypt/re-encrypt counts and rollback before retiring
an old key. Test restore before enabling credential use. Do not generate a new
key automatically when a configured key is missing or invalid. Keep only the
minimum old keys needed for retained encrypted data and the defined recovery
window. Backups contain credential material and need restricted encrypted
storage and a defined retention policy.

Railway can continue hosting the service and ciphertext. This is application
managed encryption, not end-to-end encryption or a zero-knowledge vault: the
trusted Cloud broker and destination Browser Host must handle plaintext briefly.
A compromise of a runtime that can obtain the decryption key defeats this
boundary. A managed secrets/KMS provider or external password manager is a
future backend behind a small storage interface, not a reason to promise full
protection from the current application administrator or host.

Suggested records (names are proposals, not existing schemas):

- `vault_items`: owner, immutable ID, kind, nickname, exact HTTPS origin, revision,
  key ID, ciphertext, enabled/deleted state and timestamps.
- `vault_grants`: item/owner, selected live Bot identity and lifecycle revision,
  allowed operation and grant revision. No name/role-based access or default
  grant to all Bots. Existing explicit policy denies still win.
- Existing task/review records plus a narrowly scoped use claim: item and grant
  revisions, owner, execution Bot, task, host, profile/session, verified origin,
  form identity, expiry, nonce and outcome. No secret or password-derived hash.
- Redacted audit metadata: create/replace/grant/revoke/use outcome and stable
  correlation IDs. Neither values, raw provider responses nor submitted forms.

The model may list only permitted non-secret item descriptors and request use of
an opaque reference. There is no `get_secret`, `reveal_password`, arbitrary HTTP
header injection, `op read`, environment injection or general Shell interface.
A provider-key reference resolves only inside its existing trusted adapter for
its bound endpoint/resource. It must not enable a model to choose an arbitrary
credential destination or weaken current outbound privacy rules.

## Login execution contract

1. Create the task through the existing store. Resolve an owner-owned item and
   selected execution Bot. Intersect item grant, live Bot policy, allowed site,
   host enrollment, profile ownership and current resource availability.
2. The host inspects a supported visible login form without a credential. Bind
   its exact HTTPS scheme/host/port and top-level/frame identity to a short-lived
   review. Validate the form destination too. No wildcard subdomains, HTTP,
   userinfo URLs, lookalike hostname matching or silent cross-origin fallback.
   Account selection must be explicit when more than one login matches.
3. On approval, claim use once and revalidate current item/grant/Bot revisions,
   task state, host connection, page/document/form identity and exclusive lease.
   Revocation before delivery blocks use. Revocation after delivery cannot erase
   bytes already given to a host or website; record that boundary honestly.
4. Add an explicitly versioned secret-fill exchange on the authenticated host
   channel. Only references and fixed result categories use the durable dispatch
   path. Transfer a decrypted value only over the private authenticated delivery
   path for that active use; never via task JSON, model context, process command
   arguments, broad environment variables, ordinary operator replies or audit
   events. Do not reuse the Messages connector RPC as a general secret channel.
   An old host without this capability offers manual takeover rather than
   receiving a literal password typing action.
5. The trusted host adapter performs the final live origin/form checks, fills
   and submits the explicitly approved login form. It suppresses screenshots,
   page text, DOM dumps, recordings, traces, console/network logging and detailed
   exception output during the sensitive section. Confirm no credential reaches
   model-visible tool output, source URLs or artifacts. A hidden password input
   alone does not protect screenshots or a site that echoes submitted values.
6. A site adapter verifies a safe logged-in state before releasing ordinary
   browser observation. Keep credentials available only as long as needed for
   the bounded fill/scrubbing lifecycle; Python/JS memory is not guaranteed to be
   immediately zeroed. Trusted-site behavior remains an assumption. Unsupported
   or echoing pages cannot be advertised as model-blind and require takeover.
7. Record only verified/failed/unknown outcome and references. Lost delivery or
   submission acknowledgement is not permission to replay a login or re-fill a
   password (failed attempts may lock accounts). Restart/reconnect must not
   reissue an accepted use. Resume with a safe session check or manual takeover.

The website itself necessarily receives its password. Preventing model access
is a boundary between Kyrex's model and its trusted credential executor, not a
claim that secrets never enter browser memory or that malicious destination
sites cannot leak them. Do not advertise arbitrary-site secure fill before the
output suppression and supported-form tests pass end to end.

Vault permissions govern credential use. They do not revoke an already
established website cookie. Keep profile access and site policy aligned with
item grants; do not share profiles across Bots simply to avoid sign-in. Deleting
an item must offer accurate guidance: it prevents future fills, while explicit
website logout/session revocation is a distinct operation.

## Proposed implementation sequence

| Slice | Deliverable | Release gate |
| --- | --- | --- |
| 1. Store and management | Independent keyring, transactional records, owner-only authenticated CRUD/metadata API, selected-Bot grants, web/mobile Settings view, redacted lifecycle audit | No plaintext read API; denied cross-owner access; locked state and rotation/restore tests. Private preview only until slice 2 works. |
| 2. One login end to end | Secure card, current-task review/use claim, versioned host delivery, one supported form adapter, observation suppression, verified sign-in and manual fallback | Exact-origin/form, revoked grant, replay/restart, and transcript/log/artifact leak checks pass in a controlled real browser. First public Vault release. |
| 3. API-key references | Existing provider/connector credentials managed by references with adapter-only resolution, independent scope and replacement behavior | No arbitrary shell/env access or destination substitution; old account/model connections and missing-key behavior preserved. |
| 4. Recovery and rollout | Tested encrypted backup/restore, key rotation drill, host capability downgrade behavior, deployment notes and observed live sign-in | Cloud/Chat plus Browser Host update required for secure fill. No new Android APK is required for web Settings or secure forms. |

The release includes both initial store/management and actual secure use. A
password table with an attractive Settings page is not by itself the planned
capability. Existing connections and saved browser sessions remain available
throughout; do not destructively migrate user secrets or merge profiles.

Management endpoints should accept the existing authenticated owner from the
session, never a body-supplied owner. Enforce same-origin/CSRF protection for
cookie-authenticated mutations, narrow explicit native/desktop scopes if later
needed, request-size limits, `Cache-Control: no-store`, fixed redacted validation
errors and rate limits. Framework validation must not echo submitted passwords
in an `input` field. Do not expose raw request bodies to middleware/APM logging.
A browser session is not a grant for a Bot to read or edit every stored secret.

## Acceptance checks

- Two owners, guessed item IDs and copied ciphertext cannot cross boundaries.
- An ungranted, stopped, deleted/recreated or explicitly restricted Bot cannot
  use a credential. Delegation preserves the actual execution identity.
- Replacement, disabling, grant edits, expiry, cancellation and host/profile
  rebinding invalidate pending claims. Concurrent approvals redeem one claim.
- Origin/lookalike/port/redirect/frame changes, form changes and manual-viewer
  contention block delivery; no secret is sent to the wrong host or form.
- Replay, disconnect and worker/host restart do not re-fill or resubmit a login.
- Distinct inert canary values never appear in requests to the LLM, transcript,
  memory, SSE, task DB, bridge queue, process arguments, logs, error responses,
  screenshots, recordings or exported artifacts. Test a hostile page that
  echoes credentials and ensure output stays withheld/manual fallback occurs.
- Missing/wrong keys report locked data without deleting it. Rotation preserves
  every readable record and scopes; interrupted rotation can recover. Restore
  works with the documented key set, and retained backups cannot resurrect
  grants unnoticed during recovery.
- An active browser session may survive item deletion; the UI accurately
  distinguishes disabling future fills, browser logout and upstream revocation.
- Existing provider/Calendar/mail/Messages reviews, account isolation and browser
  manual controls retain their current behavior. No new paid/write authority.

## External references

Reviewed 2026-11-03. These document product behavior and library guidance; they
are not evidence that Kyrex already implements it.

- [Grok Bot Vault](https://docs.x.ai/grok-bot/vault): secure forms and fill by
  reference; built-in items currently span an account's Bots. Kyrex's proposal
  uses explicit selected-Bot grants and preserves existing action reviews.
- [OWASP Secrets Management](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html): least privilege, lifecycle, audit, key custody and tested backup/restore.
- [cryptography Fernet / MultiFernet](https://cryptography.io/en/stable/fernet/): authenticated encryption and supported key rotation. Cryptography does not replace endpoint authorization or output isolation.
