# Budget Bot

## Private bank totals with Plaid

Settings → Budget Bot → **Private bank budget** connects one bank login per
owner, with checking, savings and credit-card account selection. The owner
completes Plaid Hosted Link, returns to Kyrex, selects accounts, a start date,
an optional starting cash balance, timezone and check frequency, and explicitly
saves tracking. Connecting a bank alone does not enable collection or inference.
The start date cannot precede the 30-day history requested when connecting. Available history varies
by institution; a complete API sync is not proof that the bank supplied every
transaction. Additional bank logins and reconnect-in-place/update mode are not
implemented in this slice.

The host requests only the Transactions product, without optional/additional
products. The protocol adapter exposes a fixed set of Link, account, transaction
sync and item-removal endpoints. It exposes no payments, transfer, Auth, Identity
or bank-statement API. Plaid's API uses POST even for reads; endpoint semantics,
not the HTTP method, define this boundary. `/accounts/get` may supply balances
and names, but Kyrex discards them. Bank credentials are entered in Plaid/the
bank's sign-in flow, never in Kyrex chat.

### What reaches the model

Raw provider responses are not model inputs. A code normalizer creates new
records containing only hashed protocol references, a validated posting date,
integer minor-unit amount, supported currency, account kind, pending flag and a
fixed category enum. Merchant names, locations, owners, descriptions, account
names/numbers/masks, bank-provided balances and arbitrary provider metadata are
discarded. Account choices use generated labels such as “Checking 1”; enable
account selection in Plaid's Link customization to restrict the upstream grant
as well. Only the chosen accounts contribute to the budget.

A second code boundary constructs daily/monthly category totals, income totals,
two seven-day comparison periods, pending count, freshness/completeness flags,
and the optional owner-entered cash estimate. No individual records, native
IDs or connection credentials are included. Unknown categories become `other`;
unsupported currency/precision and malformed dates/statuses stop the sync
without advancing its committed cursor. This is data minimization, not anonymous
data: amounts, categories and dates are still financial information.

Saving private mode pauses the owner's email receipt watcher and narrows the
selected Budget Bot to `budget:read` with all other operations denied. Its
private Chat path runs before mail, location, vision and other native routers.
It uses the existing Bot provider resolver and provider adapter for one
tools-disabled interpretation of host-computed totals, without a second agent
loop, engine history, saved memory or repository context. Only earlier messages
marked by that private Chat path may supply follow-up context. The visible Bot
conversation is reused; old receipt replies remain visible but are not replayed
to this model. The provider session identifier is also separate. User-typed
content is still sent to the selected model. Data already sent to a provider,
and another Bot's independently authorized email access, cannot be recalled or
prevented by this mode. Choose a provider whose retention terms you accept;
this feature does not certify zero-day retention.

The `budget_read` host tool also returns this aggregate schema whenever private
mode is configured, including for other Bots with a current budget-read grant.
Disconnecting keeps this privacy mode in force: reads report disconnection
instead of falling back to the old receipt ledger. Old receipt scanning cannot
be re-enabled through its settings API while this mode is active.

### Accounting and recovery

Posted charges minus posted refunds contribute to spending; pending items are
excluded. Transfers and debt payments are excluded from spending to avoid
counting card payments twice. Correctness still depends on the bank/provider
categories. Income remains separate and currencies are never combined. Card
purchases affect spending; the optional cash estimate uses **only checking and
savings** changes in the chosen currency. Enter their combined balance at the
beginning of the chosen start date. All posted cash changes, including transfers
and debt payments, affect that estimate. It is not a live balance, card equity,
bill forecast or safe-to-spend guarantee.

The existing `budget_watch` executor and CloudTaskStore/worker poller schedule
bounded bank tasks. Up to ten pages are fetched per task. Normalized updates are
staged encrypted across tasks and published atomically only after the complete
page batch finishes. A pagination-mutation response discards staging and restarts
from the original committed cursor. Pending-to-posted transitions, modifications
and removals are applied by hashed transaction identity, never receipt matching.
Bank and email records are not combined. Account-selection changes clear the
old normalized bank ledger and restart the cursor under the configuration lock.
Pausing, scope changes, cancellation and disconnect fence stale task commits.
One blocked owner cannot prevent other owners' scans. No model runs during sync;
generic task results contain only counts/status, never amounts or merchants.

Access tokens, Link state, cursors, normalized records and starting balances use
the existing connector encryption in the shared task database. Bank tokens are
not exposed to engine processes, models, UI status views, logs or transcripts.
Server credentials and encryption material remain deployment secrets. This is
trusted-backend encryption, not zero knowledge or a separate hardened vault.
Keep the deployment encryption key stable and protect database backups.

Disconnect stops tracking before calling `/item/remove`, clears the local
normalized ledger, and invalidates in-flight syncs. If remote removal fails,
the backend retains only the encrypted credential/binding needed to retry and
reports pending revocation; it never claims verified provider removal. Unknown
one-time token exchange is not automatically replayed: inspect/revoke Kyrex in
the [Plaid Portal](https://my.plaid.com/) before starting again. Existing Chat
messages, backups, and Plaid/bank retention are separate from local deletion.

### Deployment and verification

Set these on the shared web/worker deployment, outside Git:

| Setting | Value |
| --- | --- |
| `KYREX_PLAID_ENV` | `sandbox` (default) or explicitly `production` |
| `KYREX_PLAID_CLIENT_ID` | Plaid application's client ID |
| `KYREX_PLAID_SECRET` | Secret for the exact selected environment |
| `KYREX_PLAID_RETURN_URL` | HTTPS Kyrex home URL, e.g. `https://your-kyrex.example/`, with no query/fragment |

With missing/invalid configuration, Settings reports setup required. Sandbox
is clearly labeled and cannot silently become a live connection. Stored grants
are bound to the deployment client ID/environment and fail closed if either
changes. Hosted Link lets the same web application handle browser and Android
web-based surfaces without an extra SDK/APK; the actual bank-app handoff and
return still require real-phone verification. This implementation polls
`/link/token/get` on the authenticated return/check button; it accepts no public
webhooks or browser-supplied public/access tokens. The one-time exchange is
claimed durably before its external call and is never blindly retried.

Provider references:
[Hosted Link](https://plaid.com/docs/link/hosted-link/),
[Transactions integration](https://plaid.com/docs/transactions/add-to-app/),
[Transactions API](https://plaid.com/docs/api/products/transactions/),
[data consent](https://plaid.com/docs/link/data-transparency-messaging-migration-guide/).
Configure Plaid's personal-finance use case and minimal consent disclosures,
review its live-account access terms, then test Sandbox before connecting a
real bank. Automated fixtures verify protocol, privacy, arithmetic, isolation
and recovery; they do not establish production approval or a live bank outcome.

## Existing email receipt mode

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
