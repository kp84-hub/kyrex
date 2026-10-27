# Calendar event review

In Kyrex Chat Settings, open **Calendar event review**, choose a running Bot
with a configured model, choose selected inboxes and/or synced phone messages,
and enable it. Google Calendar read and write connections are required. The
default interval is hourly; 2-hour, 6-hour and daily checks are also available.
No deployment token or additional VPS watcher is needed. The existing worker
submits scans into CloudTaskStore and runs them through TaskWorker/serve.

The first scan checks content received in the previous 24 hours. Later scans
check unseen recent content, with a seven-day recovery limit. Inbox selection,
phone enrollment and Calendar destination are pinned when settings are saved.
Changing a source, account or destination requires saving settings again;
existing reviews expire. Pausing prevents further reads and invalidates approvals.

Suggestions appear in the **Calendar suggestions** conversation and existing
notification inbox. Device push uses the existing per-browser notification
consent. Empty hourly scans do not notify. Each card shows the source excerpt,
received time, coverage, event facts, destination account and calendar. Review
the facts, edit missing details if needed, and choose **Review calendar event**.
This prepares a task-scoped exact-payload approval; **Approve** is the separate
explicit final action. Dismissed suggestions stay dismissed across restarts.

The configured Bot's existing engine provider extracts quoted evidence with
tools disabled. Host parsers validate dates/times, anchor relative dates to the
message timestamp, and refuse invented durations, missing meridiems, foreign
timezone assumptions and ambiguous DST times. Cancellations and reschedules
are flagged for manual review of the existing Calendar event, rather than
creating replacements. A later same-title change blocks an older pending create.

Calendar duplicates are checked in the exact selected destination before
review and again after approval. Source content, owner/Bot restrictions,
connection revisions, task lifecycle and the approved event revision are
revalidated before creation. Calendar writes use the existing connector,
its expected-account binding and a deterministic provider event ID. A durable
attempt marker is saved before the provider request; interrupted/unknown writes
are shown as unknown and cannot be automatically retried or edited into a retry.

## Coverage and limits

- Email uses current selected connected mailbox adapters; each scan lists at
  most 50 recent INBOX messages per mailbox. It reports incomplete paging.
- Phone scans use the consented encrypted text snapshot: up to 100 messages
  from 10 recent conversations. A snapshot older than 24 hours is reported
  unavailable. Messages without a reliable received timestamp are excluded.
- Browser-only Messages connections, attachments, images, audio and older
  phone history are excluded. No new phone install or message-send consent is
  required for an already connected snapshot source.
- One tools-disabled model request processes at most 20 possible-event sources
  and 60,000 characters per scan. Remaining unprocessed candidates stay unseen
  for a later scan; coverage notes identify the limit.
- Times use the existing Calendar writer's `America/New_York` capability.
  Multi-day events, guests and recurrence require manual Calendar entry.
- Draft/source data and source bindings are sealed with the existing connector
  key. Observation identities and task links are owner-scoped in the existing
  task database. No second execution queue or automatic approval is introduced.

Validation uses fake providers and offline tests; successful tests do not imply
that production mail/phone scopes or a live Calendar write have been verified.
