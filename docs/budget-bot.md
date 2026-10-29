# Budget Bot: new-email spending and subscription tracking

This is an email-derived ledger, not a bank connection, balance, complete budget,
or subscription cancellation service. No historical import runs when enabled.

In Kyrex Chat Settings, open **Budget Bot**, choose an owner-configured provider
and model, and create the dedicated Bot. Creation uses the existing Bot API and
does not start email scanning. Existing running, stopped or paused Budget Bots
are offered for reuse rather than creating one on every visit. Select the Bot,
timezone, check frequency and meaning of unqualified dollar amounts, then enable
tracking and save. The monitor uses the selected accounts from Connections →
Email accounts; it supports the existing Gmail, Zoho and AgentMail read adapters.

The existing CloudTaskStore/TaskWorker executes every scan, including after the
Chat tab closes. The existing worker poller submits due scans; no new queue,
thread, service token or deployment feature flag is required. Both web and worker
must deploy the same code with their normal persistent data store. Checking one
page per task bounds work; continuation pages and remaining selected inboxes
run promptly before waiting for the configured interval. A busy worker may delay
checks. Settings show the last scan, errors and coverage. Collection is quiet;
there is no push notification for every receipt.

Enabling/resuming and saving after an inbox binding change starts a new cutoff
at that moment. Mail received while paused is not backfilled. An already-started
model request may finish after pausing, but settings revision checks prevent that
scan from adding records after the pause. The text reader's Date header is the
cutoff/date fallback; it is not independently verified payment
or delivery time. Missing or invalid dates are skipped. Provider searches use
date windows and supported INBOX readers. Each source is keyed by owner, mailbox
and native message ID. Failed extraction does not advance observations or the
sealed continuation. Reconnecting/removing/selecting accounts invalidates the
old binding; it never silently substitutes an inbox.

English receipt-related subjects/snippets select candidate bodies. Each candidate
contributes at most 6,000 text characters and the batch at most ten emails.
Attachments, spam/trash, nonmatching subjects, delayed/backdated messages outside
the overlapping search window and provider search limits can leave gaps. There
is no claim that all purchases or subscriptions were found. Records default to
the email date in the saved timezone unless a printed ISO transaction date is
quoted; the owner can correct dates. Changing timezone affects new records only.

Candidate text goes to the exact selected Bot model/provider through the existing
provider adapter, with tools disabled and bounded inference time. It is untrusted
data. Only exact quoted merchant/amount/currency/date/reference/interval/evidence
fields are accepted. Quote validation is not proof that an email is authentic or
that the model interpreted payment semantics correctly. The owner's review UI
shows the original excerpt and provenance, and permits corrections, exclusion
and deletion. No email is modified and no link is followed.

Completed purchases and subscription charges count toward spending; completed
refunds subtract from spending in their recorded month. Renewal notices, trials
and cancellations do not count as payments. Unknown totals/currencies and likely
duplicate receipts without an order reference need review and remain outside
totals. Matching merchant/order reference/amount/currency suppresses repeated
receipts across inboxes (charge/refund identities remain separate). Merchant
identity normalization and order-reference semantics are conservative heuristics,
not a bank reconciliation guarantee. Manual corrections never change source mail.

**Spending and subscriptions** shows monthly totals by currency and category,
records and the latest observed subscription status per merchant. Different
plans/accounts from one merchant are not a complete subscription inventory;
there is no inferred monthly bill or guarantee of current active status. Amounts
are integer minor units; USD, CAD, AUD, EUR and GBP have supported two-decimal
entry. Other currencies need manual review. Upcoming renewals are never spending.
Only 200 entries/subscriptions are displayed; totals use the full saved month.

**Open Budget Bot** reuses one owner-bound conversation. Ask “What have I spent
this month?”, “Show my subscriptions”, or “Where is most of my spending?” The
host-mediated `budget_read` tool uses the current authenticated owner and rechecks
the live Bot policy. It returns full-month aggregates and at most 50 compact
entries/subscriptions, with truncation and coverage flags, without receipt
excerpts. Model replies cannot edit records or cancel subscriptions. Bot names
and prompts grant no access: the dedicated policy grants budget/mail reads and
denies other operations. Chat's repository read-only floor is unchanged.

Configuration, receipt fields, quoted evidence and provider cursors are sealed
with the existing connector encryption in the shared task database. Native source
references are hashed for observations. Generic task results contain counts and
coverage only, not merchants, amounts or email text. Ledger data remains until
the owner deletes it; deletion clears the current entry payload and leaves only
an opaque dedupe tombstone so later scans do not restore it. Database backups
and existing Chat/provider history follow their own retention; deleting a ledger
record does not erase a prior conversation or provider request.
