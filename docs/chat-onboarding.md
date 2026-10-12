# Kyrex Chat first-run setup

New accounts configure their own LLM before the conversational assistant can
run. Setup is a deterministic UI/API flow, not an LLM prompt. The wizard reads
only the authenticated owner's encrypted provider profiles and Bot registry;
server-wide provider environment variables do not satisfy first-run readiness.

1. Choose OpenCode Go, OpenRouter, OpenAI, or Anthropic and enter credentials in
   the password field, or select an existing account-owned profile. Saving
   credentials does not run an LLM. URLs for new wizard profiles come from the
   server's provider presets rather than a client-supplied endpoint.
2. Select a model from the provider catalog or enter its exact ID. Catalog
   failure leaves manual entry available. The model list is a convenience list,
   not an allowlist, and native suffixes such as `vendor/model:free` are preserved.
3. Explicitly test the model. This sends one short request with a 64-token output
   budget, no tools and no personal conversation data. Provider billing or its
   included allowance applies. Redirects are refused; responses, timeouts and
   repeated tests are bounded. Failures do not replay requests or expose keys
   or provider debug bodies.
4. Click **Create Overwatcher and open Chat**. A successful test is bound to that
   account's current provider, credentials and model for 15 minutes. Changes
   require another test. The new Bot references the encrypted profile and uses
   the existing coordinator policy and owner boundaries. It starts running and
   gets one welcome conversation. Repeated completion requests reuse it.

The welcome message asks what the user wants help with first. Its system prompt
guides conversational personalization, relevant connections, and optional voice
setup after the chat provider works. It does not assume the owner's name,
family, connected services, or tasks, and directs secret entry to settings.

Progress survives refresh and server restarts in `chat_onboarding.sqlite3` under
the persistent `KYREX_DATA_DIR`. Keep `WEB_SESSION_SECRET` (or
`KYREX_PROVIDER_SECRETS_KEY`) stable so encrypted profiles remain readable.
Provider credentials use the existing encrypted account profile store; setup
state keeps profile references and keyed verification digests, never keys.

Existing owned, configured Overwatchers skip the wizard and retain their model,
prompt, permissions and lifecycle state. Custom endpoints remain available
through the existing **Settings** and **Bots** advanced flow; the wizard's
connection test targets the listed first-party preset endpoints.

APIs live under `/api/chat/onboarding`: status, provider selection, model catalog,
explicit test, and completion. Every call is owner-authenticated; browser writes
check the origin, and other users' profiles cannot be selected.

Validation: `test_chat_onboarding.py` covers owner isolation, encrypted drafts,
protocol selection, expiry/credential rotation, explicit probe failure, idempotent
completion and existing-account preservation. `onboarding.test.mjs` exercises the
visible wizard and the actual App gate with advertised global models. CI runs
these alongside provider/Bot regression tests. Automated tests use mocked provider
responses; they do not spend credits or open microphones.
