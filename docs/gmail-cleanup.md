# Reviewed Gmail cleanup

In Chat Settings, use **Gmail cleanup → Connect Gmail cleanup**. Choose the
Google account deliberately and verify the displayed cleanup mailbox afterward.
The OAuth callback uses the existing configured Google client and callback URL,
PKCE and a single-use owner-bound `trash_` state. The OAuth app must support the
requested `gmail.readonly` and `gmail.modify` scopes.

Google's `gmail.modify` permission is broad: it also permits reading and sending.
Kyrex exposes only metadata reads and an exact reviewed Trash operation on this
connection. Its credential file is separate from existing Calendar/Gmail readers;
their OAuth scope allowlist and tokens are unchanged. Budget Bot's existing
deny policy prevents using it as an email cleanup assistant. Choose an eligible
personal assistant such as The Overwatcher, or ordinary non-workspace Chat.

Ask **Delete all emails from offers@example.com**. A brand-name request such as
**Delete all emails from Home Depot** performs a bounded sender lookup and asks
you to choose one actual sender address. It does not infer that all similar
domains or display names belong to the same business. Only the current owner
request, or an immediate exact-address reply to that question, starts a review.
Email content, quoted instructions and model output cannot approve cleanup.

The existing CloudTaskStore/TaskWorker prepares the preview and owns its
approval. Closing the page does not approve anything or resubmit the task.
The card shows the exact mailbox, sender, message count and every selected
subject/date. It covers at most 100 messages and clearly reports when more
search results remain. The search includes archived mail; it excludes Trash,
Spam, Sent and Drafts. It checks each actual From address rather than trusting
Gmail search aliases. Message bodies and attachments are never requested by
this flow, and no LLM inference is used.

**Move listed emails to Trash** approves only the frozen message IDs, mailbox
grant revision, conversation, review revision and current approval ID. New
arrivals are not added. The worker rechecks ownership, Bot restrictions, task
claim, account identity, expiry and each message's sender/header fingerprint
before the one write. A changed account or message blocks the whole operation.
Reviews expire after 30 minutes. Cancellation or declining submits no write.

The only write is Gmail `messages.batchModify` with those exact IDs,
`addLabelIds: [TRASH]` and `removeLabelIds: [INBOX]`. No permanent-delete, generic
label mutation, send, whole-thread delete or scheduled cleanup is exposed.
The worker claims the attempt durably before the provider request and verifies
TRASH labels afterward. A crash, timeout or unverified provider result stays
unknown and cannot be replayed automatically. Check Gmail directly before
preparing another cleanup. Already-started operations may finish after a
disconnect; disconnect prevents future authorization and invalidates old reviews.

Preview metadata is encrypted with the existing connector sealing under the
shared task database. Approval events bind the exact manifest digest rather
than copying subjects into the shared task log. Chat retains the review ID and
generic preparation reply. This manual cleanup is independent of Budget Bot's
Inbox-only receipt scans and Gmail filters.

Deploy both web and worker from the same revision. No new worker, APK or model
is required. Granting Google permission is setup only: every cleanup still
requires its separate exact-message approval.

References: [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes),
[batchModify](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/batchModify),
and [system labels](https://developers.google.com/workspace/gmail/api/guides/labels).
