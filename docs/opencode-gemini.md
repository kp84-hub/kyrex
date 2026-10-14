# Gemini models through OpenCode

Keep your existing OpenCode provider profile and select the exact model ID,
such as `gemini-4-argon-contributor`. Kyrex automatically uses Gemini's native
streaming protocol for `gemini-*` IDs on the OpenCode gateway. No separate
Google key or manual change to the Bot's provider endpoint is required.

Existing Go profiles using `/zen/go/v1`, `/inference/go/openai/v1`, or
`/inference/go/v1` send Gemini requests to the documented Google SDK route:
`/inference/go/v1/models/<model>:streamGenerateContent?alt=sse`.
The existing API key, custom headers, and stable per-conversation
`x-opencode-session` stay in request headers. Unknown gateway paths fail;
Kyrex does not try another account or provider after a failure.

The adapter converts system instructions, text/inline image input, tool
declarations, calls, and results into native Gemini JSON. Signed response
parts survive saved engine sessions and parallel tool follow-ups. An
interrupted, truncated, filtered, or malformed response never returns an
executable tool call. Native Gemini metadata is excluded from requests to
Chat Completions providers; other gateways keep their configured protocol.

When importing tool history from another model or an older saved session,
Kyrex uses Google's documented `skip_thought_signature_validator` migration
sentinel on the first call in each batch. It preserves actual Gemini signatures
and never substitutes the sentinel for a new, unsigned Gemini response. A new
Gemini 3+ call missing its required signature fails before tools can execute.

The first-run model test also uses the native Gemini endpoint, with only a
fixed connection-test prompt and no tools or conversation history. It avoids
a tiny output-token cap that can exhaust the model's thinking budget before
it returns an answer. A successful test establishes basic account/model access;
it does not establish that a particular conversation or tool request succeeds.

For rejected native requests, Kyrex reads at most 16 KiB of the error document
and exposes only recognized machine codes such as `inference_failed` or
`INVALID_ARGUMENT`. Raw provider messages, prompts and credentials stay out of
the displayed error. `inference_failed` alone does not identify the cause;
the console's HTTP 400 is insufficient to distinguish history validation,
account/model availability or an upstream failure. No automatic retry occurs.

Tests intercept actual HTTP requests without provider credentials. Live
availability, quota, and account access still require an authenticated user
test after deployment. OpenCode describes the Argon Contributor model as
allowing prompts and completions to train Google models; existing local
credential filtering does not make that service private or anonymous.

In Bots → Configure LLM, choose your saved OpenCode profile and model, then
press **Check OpenCode connection**. This checks the authenticated Console
workspace config at `/console/api/v2/config` and sends one short prompt through
the same provider adapter used by Chat. It uses your saved credentials server-side
and sends no conversation history or tools. The check never saves a model,
creates a task or changes a Bot's lifecycle. It may consume provider allowance;
checks share a five-per-minute owner limit with first-run setup.

A workspace listing is catalog evidence, not proof of successful inference.
A successful minimal request establishes that the account and model can answer
that request. If Chat still fails, investigate its schemas and history. If the
minimal request also returns `inference_failed`, changing the Bot's conversation
history is insufficient to explain it. The error still does not establish a
specific account or provider cause. An unavailable config check is reported
separately; it does not prevent the direct model test.

The model IDs in Kyrex are bare API IDs such as `gemini-4-argon-contributor`.
Kyrex also accepts the OpenCode CLI's `opencode-go/<model-id>` form and strips
that provider prefix before calling Go.

Saved profiles can be changed through Settings → Edit next to the profile.
An empty API key field keeps the stored key. The profile ID remains fixed, so
existing Bots continue referencing the same profile. Extra headers default to
Keep saved headers; choose Clear extra headers to remove them, or Replace to
supply a complete replacement. The API preserves headers when omitted/null and
clears them when explicitly given `{}`. Saved header values are never displayed.

Use one OpenCode Go profile for all Go models. The canonical base is
`https://opencode.ai/inference/go/v1`; existing `/zen/go/v1` and
`/inference/go/openai/v1` profiles are normalized at runtime, including an
accidentally saved `/responses`, `/messages`, `/models` or `/chat/completions`
suffix. Other gateways and non-Go OpenCode endpoints retain their behavior.
Kyrex selects Chat Completions, Responses, Messages or Gemini from the model
family. Claude, MiniMax and Qwen Go models use native Messages; GPT, Grok and
Muse Spark use Responses; Gemini uses its native Google API; the other current
Go models use Chat Completions. Go credential/session/client identity stays in
headers across every route. Imported reasoning is not fabricated as an
Anthropic thinking block; canonical tool calls/results migrate in order.

The current 32 documented Go models are suggestions alongside saved choices,
never an allowlist. Exact custom IDs remain accepted; provider availability,
quota and new protocol assignments require provider-side verification.

In Settings, a duplicate Go profile offers **Use <primary profile> instead**.
This owner-requested operation requires two Go profiles with the same full key.
It preserves the primary key and headers, combines model suggestions, updates
only the owner's Bot profile references without changing models or permissions,
and atomically removes the duplicate profile while retaining an internal alias
for saved chats and legacy references. Different keys, gateways or owners are
rejected. Repeating a completed consolidation is safe. No live profile is
changed merely by installing the release.
OpenCode Gemini does not need headers literally named `Key` or `Value`:
Kyrex supplies bearer authentication and its managed conversation session.
Removing accidental extra headers gives a clean configuration to test; it
does not establish that those headers caused an earlier HTTP 400.

Provider references:

- [OpenCode Go model endpoints and privacy](https://opencode.ai/v2/docs/console/go/)
- [OpenCode inference API](https://dev.opencode.ai/v2/docs/console/api/inference/)
- [Authenticated workspace config](https://opencode.ai/v2/docs/console/api#workspace-config)
- [Gemini Generate Content API](https://ai.google.dev/api/generate-content)
- [Signed tool history](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures)
