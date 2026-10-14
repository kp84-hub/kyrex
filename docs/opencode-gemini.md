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
