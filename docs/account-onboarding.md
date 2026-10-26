# Account onboarding and supervised learning pilot

This implementation is opt-in. All new admission paths and the managed pilot
are disabled by default. Existing GitHub login, owner keys, encrypted provider
profiles, Bots, tasks and transcripts retain their identifiers and locations.
No production credentials or data migration are performed by this change.

## Scope

Independent adults can sign in with configured Google, GitHub or email-code
authentication. First-run account setup collects a preferred name and timezone
before the existing provider/model onboarding. Optional Gmail, Calendar and
repository connections remain separate. Google login requests only `openid
email`, verifies the signed ID token/audience/issuer/nonce, and discards tokens.

Each new account has a random permanent ID and a reserved `account_<uuid>` owner
key. Upstream identities map to that ID. Existing operator data keeps its old
owner key; the first GitHub registration binds its immutable upstream user ID.
Once bound, an unrelated user cannot claim it by obtaining the old GitHub name.
Matching email addresses never merge accounts. An authenticated owner can link
an additional Google, GitHub or email sign-in in Settings after proving that identity.

New accounts cannot use deployment-wide provider keys, workspaces, ownerless
Bots or `/api/task`'s operator repository. Developer Bot connections continue to
use the existing owner-scoped repository workflow. Managed accounts cannot
reach that workflow.

The supervised pilot supports **text conversation and coding explanations**.
It does **not** support running code, filesystem access, connected email or
calendar, Bots, tools, attachments, voice, saved memory, purchases or automations.
Its adapter uses existing Kyrex model providers with `tools=None`; requests do
not enter the agent loop, intent shortcuts or connector routing. This is an
account boundary, not a prompt-based permission mechanism.

Parents create separate managed identities, approve an exact model funded by
their own provider profile, pair devices, set daily message limits, review chats,
pause/resume access, revoke paired devices, and delete the managed chat data.
The learner sees that the parent can review conversations. Child devices never
receive the parent's session, API key, connector grants or administrative rights.
Provider references stay in the parent account; revoking the provider/model grant
blocks future turns. Message limits are **not monetary spending caps**.

## Configure a closed rollout

Back up the persistent data volume first, and keep a stable `WEB_SESSION_SECRET`
or `KYREX_PROVIDER_SECRETS_KEY`. Preserve existing GitHub OAuth variables and
`WEB_ALLOWED_GITHUB_USERNAME` for the operator. Then set:

```text
KYREX_ACCOUNTS_ENABLED=1
KYREX_SIGNUP_MODE=invite
```

Use Settings → Account → Invite someone to allow a specific email address or
`github:username` for seven days. This records admission; it does not send an
invitation message. An existing invited identity can return without another
invitation. `open` admits new verified identities publicly and should not be
enabled for this pilot. A general public launch still needs broader per-tenant
resource/quota and external-write testing described in the platform audit.

For Google sign-in configure a web OAuth client with the exact callback:

```text
https://<chat-host>/auth/google/callback
KYREX_LOGIN_GOOGLE_CLIENT_ID=<web-client-id>
KYREX_LOGIN_GOOGLE_CLIENT_SECRET=<web-client-secret>
```

These settings are deliberately separate from Gmail/Calendar connection OAuth.
The same Google project/client can be used if its approved redirect URIs and
consent configuration include this login, but sign-in grants no service access.

For email sign-in configure a dedicated authenticated mail sender:

```text
KYREX_LOGIN_SMTP_HOST=<smtp-host>
KYREX_LOGIN_SMTP_PORT=465
KYREX_LOGIN_EMAIL_FROM=<sender-address>
KYREX_LOGIN_SMTP_USERNAME=<smtp-user>
KYREX_LOGIN_SMTP_PASSWORD=<smtp-credential>
```

465 uses implicit TLS; other ports require STARTTLS with certificate validation.
No insecure transport or email-code logging fallback is supplied. Codes expire
in ten minutes, are bound to the requesting browser, stored only as keyed digests,
consumed once and limited to five attempts. Issuance and verification are rate
limited in SQLite. A separate verified identity and a matching live owner session
are both required to link an additional sign-in method.

## Enable the managed pilot separately

Before collecting child data, establish the applicable parental notice/consent,
privacy, retention and deletion process, and verify each upstream model provider's
child-use/data terms and safety behavior. The pilot acknowledgment in the UI is
not a claim of verifiable parental consent or COPPA compliance. A parent checking
a box or supplying their own key does not override a provider's restrictions.
Do not open under-13 public registration with this implementation alone.

Only after that configuration is appropriate for the deployment:

```text
KYREX_MANAGED_ACCOUNTS_ENABLED=1
KYREX_MANAGED_MODEL_ALLOWLIST=[["<provider>","<exact-https-base-url>","<model-id>"]]
```

The allowlist is JSON and matches the saved provider, endpoint and exact model.
It is checked when the parent selects a model and again during chat. An empty
allowlist enables no model. Custom endpoints are not implicitly trusted. All
provider keys stay encrypted in the existing parent provider-profile store.

Pairing links put a 256-bit, ten-minute, single-use code in the URL fragment.
The welcome screen removes it from browser history and requires an explicit
pairing action. No child email is required. Parent pause blocks access immediately;
revoking devices advances the account session epoch, revokes browser sessions,
invalidates pending pairing codes and rejects stale native handoffs/tokens.
Kbot uses the existing S256-protected native exchange after the browser login or
pairing flow; its installed binary does not need a new password store.

Deletion pauses the account, revokes sessions and removes its local Chat folder,
including transcripts. A tombstone retains the random account ID and guardian
relationship to prevent reuse. Model-provider copies are not deleted by deleting
Kyrex's records; follow the provider's retention/deletion process. Do not treat
this as a complete backup erasure or an export-compliance implementation.

## Compatibility and limitations

The existing engine, policy, task store, approvals, connectors and GitHub-native
IDE login remain in place. The Chat/Kbot account gate runs before Chat's provider
onboarding and never calls an LLM to configure an LLM. Existing configured owners
skip first-run setup. The selected account name/timezone are saved explicitly;
this change does not migrate every existing feature's domain-specific timezone
default. Confirm scheduling details through the existing calendar review.

Standalone local TUI and an unrestricted PC are outside cloud supervision. Do
not give a managed learner the guardian's existing local TUI credentials or
claim that pairing Kbot controls local programs. Cloud-connected TUI management,
safe coding execution, guardian approvals for external actions, additional
guardians, account graduation, monetary budgets and general team sharing remain
future extensions to this account model.

SQLite registries share the existing single-volume deployment topology. Native
handoffs/tokens remain process-local and can require sign-in after restart; do
not introduce replicas without migrating that state to transactional storage.
Roll back the managed flag first, then the account flag. New accounts lose access
when disabled; the legacy operator's existing GitHub path remains usable. Do not
delete/rename account stores as a rollback technique.

## Verification

`test_universal_accounts.py` exercises independent owners, legacy ID preservation,
invitations, email and Google verification, replay/attempt limits, origin checks,
workspace/provider isolation, parent-child separation, revocation and native PKCE.
Managed chat tests assert that no engine or connected-service call is dispatched.
`accountOnboarding.test.mjs` checks signup/profile flow and managed UI isolation.
CI also runs the existing OAuth/session/provider-onboarding regressions and builds
the production SPA. Tests mock email delivery and Google/model calls. Live OAuth,
SMTP delivery and an installed Kbot handoff require deployment configuration and
must be verified before inviting another person.
