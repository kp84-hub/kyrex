# A dedicated email inbox for Kyrex

Give Kyrex an AgentMail inbox and forward only selected email to it. Email Bot
uses the same bounded search and read tools for this source. Calendar continues
to use its existing Google connection.

## Setup

1. Create a dedicated inbox in [AgentMail Console](https://console.agentmail.to).
   Copy its **inbox ID**; this may differ from its email address.
2. Create an API key scoped to that single inbox with only `inbox_read` and
   `message_read` permissions. Do not use an organization-wide unrestricted key.
3. In Chat → Connections → **Email inbox**, optionally choose **Stop personal
   Gmail reads** immediately. Enter the inbox ID and API key, then select
   **Connect dedicated inbox**. Kyrex verifies the inbox identity and message
   read permission, encrypts the key, and shows the verified email address.
4. In Gmail → Settings → Forwarding and POP/IMAP, add that address. Ask Email Bot
   to read the confirmation email, then verify the link yourself.
5. Create Gmail filters matching only the school senders/topics and other mail
   you want to share, with **Forward it to** the dedicated address. Leave global
   forwarding disabled. A manual forward also works for reading, though sender
   notification rules use the outer sender, which can be you for manual forwards.
6. Ask Email Bot to read or summarize a forwarded message. Notification rules
   use the existing optional email watcher; changing inbox sources starts a new
   baseline so old inbox contents are not replayed as new notifications.

The connector does not create an AgentMail account or inbox for you. Put the key
in Connections, not in a chat message or repository. It is encrypted with the
existing Cloud secret store; status responses never return it.

## Access and disconnecting

Connecting AgentMail blocks personal Gmail reads in Kyrex. Provider failure,
disconnect, or switching to **Reading disabled** never falls back to Gmail.
Disconnecting deletes the saved AgentMail credential and keeps personal Gmail
blocked. **Use personal Gmail** is an explicit source change; Gmail OAuth read
permission is also required. Existing accounts keep their Gmail source until
they make one of these choices.

This local block preserves Calendar's shared Google grant. It does not revoke
the Gmail OAuth scope at Google. To revoke it there too, remove Kyrex's Google
authorization and reconnect Calendar only. Previously saved Chat messages are
retained. AgentMail and Kyrex receive the messages you forward; those messages
may also be sent to your configured LLM when you ask it to use their content.

## Reader limits

Only GET requests to the configured inbox are supported. There are no send,
reply, delete, archive, attachment download, or organization-inbox enumeration
operations. Bodies are capped at 20,000 characters, HTML is converted to text,
and full forwarded content is preferred over extracted new-text-only content.

Search supports plain text, `from:`, `subject:`, `after:`, `before:`,
`newer_than:`, and `in:inbox` (received messages). Other Gmail-specific operators
fail with guidance. Sender notification rules check the actual outer From,
not a sender quoted inside forwarded text. Search pages are capped at 50;
continuations are owner/source/query-bound and expire after one hour.
AgentMail full-text search ranks relevance, so latest-email requests examine
at most 500 matches and fail with a narrowing prompt if the search is incomplete.

Credentials and settings are stored in `agent_inbox.sqlite3` under the Cloud
data directory; back it up alongside existing connector storage. There are no
new host environment variables beyond the existing encryption secret.

Provider references: [inbox API](https://docs.agentmail.to/api-reference/inboxes/get),
[message search](https://docs.agentmail.to/api-reference/inboxes/messages/search),
[message read](https://docs.agentmail.to/api-reference/inboxes/messages/get),
[API key permissions](https://docs.agentmail.to/permissions).
