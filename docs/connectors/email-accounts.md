# Multiple email accounts

In Chat, open **Connections → Email accounts**. Add Gmail / Google Workspace,
Zoho Mail, or AgentMail accounts, then choose the **Email search scope**:

- **Existing email reader** retains the previous dedicated-inbox / shared-Google
  source choice. This is the default, including after adding another account.
- **All connected inboxes** explicitly includes the available existing reader
  and additional connected accounts.
- An individual address limits searches to that mailbox.
- **Pause Chat email searches** disables the normal Chat search reader without
  revoking OAuth grants, changing the dedicated sender, or changing automations.

Additional accounts do not replace each other or the existing dedicated inbox.
Up to ten additional accounts are supported per owner. Removing a selected
account pauses that search selection rather than choosing a different mailbox.
If a chosen mailbox fails, the read reports the error instead of silently
substituting another scope. Existing automations remain bound to their previous
configured inbox; a Chat search-scope change does not expand their access.

## Gmail

**Add Gmail account** opens Google consent with account selection. Repeat for
each address. The existing Gmail card starts this same independent connection
flow and is no longer blocked by a dedicated Zoho/AgentMail source selection.
Kyrex requests only `gmail.readonly`, uses S256 PKCE, and verifies the actual
address using Google's `users/me/profile` endpoint before adding it to the list.
Reconnect an existing row using the same address; use Add for a different one.
An already connected additional address is not overwritten by another Add flow.

Use the existing Google connector client configuration:

```text
GOOGLE_CLIENT_ID=<existing-web-client-id>
GOOGLE_CLIENT_SECRET=<existing-client-secret>
GOOGLE_REDIRECT_URI=https://<chat-host>/api/connections/google/callback
```

No new callback registration is needed when this URL is already configured.
Google login settings (`KYREX_LOGIN_GOOGLE_*`) are separate from connector
consent. Gmail API access and the consent application's provider configuration
must be available. This code cannot bypass a Workspace administrator's policy
or Google's consent restrictions.

Each additional Gmail grant uses its own encrypted credential record. Calendar
and its write approvals continue to use the original Google record. The new
flow neither changes that record nor requests Calendar scopes. Removing a
mailbox drops its local credentials and pending callbacks without calling
Google's app-wide revocation endpoint, which could also revoke Calendar.
Google may retain the application's consent; manage provider-wide revocation
in Google Account settings if desired.

When the already-enabled legacy Gmail reader's identity can be verified, its
safe address metadata is cached against that grant's connection time. A
combined search avoids querying the same address twice when it is also present
as an additional Gmail account. A blocked legacy Gmail grant is not enabled or
used to inspect mail while adding another account.

## Other mailboxes and sending

Additional Zoho accounts reuse the existing read-only TLS IMAP adapter and its
supported US servers. Each needs its own mailbox address and app password.
Searches still cover the adapter's bounded recent Inbox window, not all folders
or all historical mail; aliases can share one underlying mailbox. AgentMail
accounts reuse the inbox-scoped read-only API adapter and credential checks.

Sending remains a separate capability of the existing dedicated Zoho inbox,
with exact From/To/subject/body review and one claimed send attempt. Additional
accounts are read-only. To reply through the dedicated sender, select and read
the message through **Existing email reader**; a message selected through a
combined/additional account search cannot silently become a reply from a
different sender. Adding Gmail does not enable Gmail sending, deleting,
archiving, or label changes.

## Search coverage and identity

Combined pages interleave bounded provider results and identify the mailbox
on each message. They are **not globally sorted by date**. For a latest-email
request choose one account, or prefix a search with `mailbox:<selected-address>`
to narrow an All connected inboxes scope; `from:` still means the sender,
not the mailbox being searched. An unavailable address is not substituted.

Requests such as "read my two newest emails" read up to five messages from
that same selected mailbox. The canonical form is `gmail: latest --limit 2`
(optionally followed by a search query); plain `gmail: latest` still reads one.
Batch reads identify each email, bound each excerpt, and require selecting one
message before a later reply or email-to-calendar handoff.

Message and continuation references are owner-bound, opaque and stored with
encrypted native IDs/provider tokens. Provider message IDs and thread IDs from
different accounts cannot collide. References are bound to the account/settings
revision; changing scope, reconnecting or removing accounts invalidates stale
results. Message references expire after seven days; combined continuation
references expire after fifteen minutes. Search again if a saved selection
expires. A queued task with a new account binding is checked before execution
and result publication, and readers recheck settings around provider calls.
Older unbound jobs continue through the existing reader, not a newly selected
mailbox group.

Back up the persistent Kyrex data directory, including `email_accounts.sqlite3`
and `email_accounts_credentials/`. Keep the existing connector encryption key
stable; adding accounts does not rotate it or migrate older encrypted records.
The registry uses the existing single-volume Cloud deployment. This does not
introduce a replicated credential store or another worker/agent queue.

Tests use fake OAuth, Gmail, and IMAP providers. They verify owner/account
isolation, same-ID collisions, pagination, consent replay, mailbox identity,
revocation, unchanged legacy connections, UI selection and secret clearing.
Real Google consent and mailbox access still require a deployed end-to-end test.

References:

- [Google Gmail profile endpoint](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/getProfile)
- [Google web-server OAuth](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)
