# A dedicated Zoho mailbox for Kyrex

Use your own domain, such as `assistant@example.com`, for the emails you choose to
forward to Kyrex. Mailbox provisioning and DNS stay in your Zoho organization;
Kyrex does not change them or reuse Chronic Cortex's SMTP credentials.

## Set up the mailbox

1. In [Zoho Mail Admin](https://mailadmin.zoho.com), add your domain, verify its
   ownership, and follow Zoho's mail-delivery instructions. If it already has
   working email elsewhere, check the existing routing before changing MX.
2. Create a **separate mailbox user** for Kyrex. An email alias shares its
   account's inbox; it does not isolate Kyrex from that account's other emails.
3. The mailbox's Zoho plan must support IMAP. Zoho's current free hosting plan
   is web-only. Enable IMAP under Zoho Mail → Settings → Mail Accounts for the
   dedicated mailbox.
4. Sign in to that mailbox's Zoho account and generate an app password under
   Security → App passwords. Enter it only in Kyrex Connections, not Chat.
5. In Kyrex Chat → Connections → Email inbox, select **Zoho Mail**, enter the
   mailbox address and app password, then choose its incoming server:
   `imappro.zoho.com` for US custom-domain organization mailboxes, or
   `imap.zoho.com` for a US personal/legacy mailbox if its server settings say so.
   The first version supports these two US servers only; do not send a non-US
   mailbox's credentials to them. Check Zoho's account-specific server settings.
6. Connect the dedicated inbox. Kyrex authenticates and opens Inbox read-only
   before saving anything. The password is encrypted with the existing Cloud
   secret store and is never returned by a settings response. A blank password
   can keep the saved credential only for the same mailbox and server.

## Forward only selected emails

Add the dedicated address in Gmail → Settings → Forwarding and POP/IMAP.
Read the confirmation in Zoho or ask Email Bot to read it, then verify it
yourself. Create filters for the school senders/topics and other emails you
want to share. Leave forwarding for all incoming mail disabled.

Ask Email Bot to read, search, or summarize those forwarded messages. Automatic
forwarding is best for sender notification rules; a manual forward can name
you as the outer sender. Quoted From lines are not used as sender authority.

## Kyrex's email identity

The active dedicated mailbox is Kyrex's email address for your account. Ask
“What's your email address?” or “What email should I forward emails to?” in
ordinary Chat or any of your Bots. Kyrex answers from current connection
settings without opening mail or calling a model. Ordinary Chat, workspace
chat, owned Bot engines and delegated Bot executors receive the same safe
identity context. Only the address, provider and read/send capability facts
are included; passwords and API keys are never included.

Changing the mailbox updates this identity on the next turn. Disconnecting or
selecting personal Gmail removes the dedicated identity; retained addresses
in settings, old messages and memory are not used as the current address.
There is no hardcoded address or global mailbox shared between users.

The current connection reads email only. Kyrex can draft an appointment
request using this identity, but sending/replying and confirming bookings
require a separate implementation. Creating a calendar event alone does not
book an appointment with a business.

## Access boundaries

Selecting Zoho blocks personal Gmail reads in Kyrex while preserving Calendar.
Provider failure or disconnect never falls back to Gmail or AgentMail.
Disconnect removes the saved credential and leaves all email reading disabled;
personal Gmail must be chosen explicitly to enable that source again.

This is a local Kyrex access block, not revocation of Google's earlier OAuth
grant. Previously saved Chat messages remain. Selected email evidence can be
sent to your configured LLM when you ask it to use that content.

Zoho app passwords are account credentials, not provider-enforced read-only
tokens. Kyrex enforces its reader boundary with a TLS-verified connection,
`EXAMINE INBOX` and `BODY.PEEK` fetches. It has no SMTP, STORE, COPY, MOVE,
DELETE, CLOSE or EXPUNGE operation. Reading does not mark an email Seen.
Use credentials for the dedicated mailbox account only.

Only Inbox is read. Search examines at most the latest 5,000 Inbox UID slots,
with pages capped at 50 matches; older messages can be checked in Zoho itself.
Continuation pages use a stable snapshot, expire after one hour, and are tied
to the owner, source revision, search and mailbox UIDVALIDITY. A mailbox reset
or source change invalidates previous references instead of reading a different
message. Latest means the newest delivered matching Inbox identity, not a
quoted original Date inside a forwarded email. IMAP date filters use Inbox
delivery dates; original dates inside manually forwarded content can be
searched as plain text.

Full forwarded inline text is retained. The reader prefers an unnamed inline
plain-text MIME part, then HTML converted to text. It does not download named
attachments or attached `.eml` messages. Headers, MIME structure, text fetches
and returned bodies are bounded; long text is explicitly marked truncated.
Malformed or oversized provider responses fail closed.

Settings use the existing `agent_inbox.sqlite3` data file. Its schema migration
preserves existing AgentMail accounts and encrypted credentials. AgentMail
remains another selectable provider. There are no new global Zoho environment
variables or host passwords to configure.

References: [Zoho custom domains](https://www.zoho.com/mail/help/adminconsole/add-domains.html),
[domain verification](https://www.zoho.com/mail/help/adminconsole/domain-verification.html),
[IMAP setup](https://www.zoho.com/mail/help/imap-access.html),
[plans and IMAP](https://www.zoho.com/mail/help/adminconsole/subscription.html),
[app passwords](https://www.zoho.com/mail/help/adminconsole/two-factor-authentication.html),
[IMAP protocol](https://www.rfc-editor.org/rfc/rfc3501).
