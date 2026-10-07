# Kyrex Chat privacy

## What reaches a model

The selected provider receives the chat history, instructions, and tool results
needed for the request. Email bodies, calendar details, messages, names, and
locations are not anonymized by this change. Provider training and retention
policies still apply. This is not a local-model or zero-retention mode.

Both provider adapters filter text before SDK submission, including past turns,
reasoning text, tool results, function-call argument strings, and tool
descriptions. The filter replaces known environment/provider credentials and
recognizable API keys, JWTs, private keys, password/token assignments, HTTP auth
and cookie headers, URL credentials, and selected signed-URL parameters with
`[REDACTED_SECRET]`. Structured JSON remains valid; tool names, call IDs, and
schemas are preserved. The source transcript is not modified.

Filtering is best effort. Unrecognized secrets, credentials in images, and
identifying personal details can still reach a model. The filter runs locally
and does not send content to another service for classification. Models are
told to use host-managed authenticated tools rather than redacted credentials.
Authentication headers for the selected provider remain intact: the model
request still needs the configured provider's API key.

OpenAI Chat Completions requests to the direct OpenAI endpoint and OpenCode
Responses requests explicitly set `store: false`. Other compatible Chat
Completions endpoints keep their existing request fields. This field does not
override gateway retention, abuse monitoring, or training policies.

## Saved memories

Settings includes **Share saved memories with models**, scoped to the signed-in
owner and persisted on the Kyrex server. Existing behavior stays enabled by
default because memories are already saved only on explicit user request.

Turning sharing off stops reading and injecting saved memories into future
requests, including the refreshed context of a reused Chat engine session.
It does not delete memories from the configured Firestore service, erase
details already present in chat history, or stop an explicit "What do you
remember?" command from displaying saved items. Users can still explicitly
remember or forget an item. A malformed preference fails closed for sharing.

## Server storage and errors

New or updated Chat transcript files and privacy preferences are atomically
published with file mode `0600` inside directories with mode `0700`. These are
filesystem permissions, not transcript encryption. Older transcript files are
updated when next written; engine histories, task/audit stores, saved memories,
and backups retain their existing storage behavior.

Provider failures and retry logs use fixed diagnoses derived from HTTP status
or known exception types. Raw upstream bodies, echoed prompts, credential-bearing
URLs, and exception text are not included. This trades some provider-specific
diagnostic detail for less accidental disclosure.

## Verification

Offline tests inspect actual SDK submission payloads for OpenCode Chat
Completions, OpenCode Responses, direct OpenAI, and Anthropic. They verify
credential removal, unchanged authentication, valid tool schemas/history,
safe errors/logging, owner isolation, permissions, and memory sharing across
normal and reused engine sessions. UI tests cover loading, persistence, and
failed saves. No live provider or connector account is used by these tests.
