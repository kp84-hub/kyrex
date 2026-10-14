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

Provider references:

- [OpenCode Go model endpoints and privacy](https://opencode.ai/v2/docs/console/go/)
- [OpenCode inference API](https://dev.opencode.ai/v2/docs/console/api/inference/)
- [Gemini Generate Content API](https://ai.google.dev/api/generate-content)
- [Signed tool history](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures)
