# Browser connection diagnostics

Connections → Browser now offers an explicit **Test browser connection** action
and saved recent tests/failures. Select a read-authorized Browser Bot and enter
an allowed public HTTPS page URL. No model inference is involved.

The test uses the existing owner/Bot binding, HostManager, durable
`browser_dispatches` queue, poller, host-side allowlist intersection and exclusive
profile lock. It reports host connection, task delivery, browser launch, website
access and member sign-in separately. Generic websites leave sign-in **Not
checked**. The existing Glofox adapter recognizes its member portal; sign-in is
only a readiness signal, never proof that a reservation will succeed.

The host uses a task-owned browser context with service workers blocked. During
the test, network interception blocks non-read HTTP methods (except the existing
adapter's exact credential-free Glofox guest bootstrap), action URLs,
off-list requests and WebSockets. It never clicks or fills controls, captures
screenshots, reads cookies, submits a booking, resets profiles or rotates enrollment.
Contexts with existing service workers or externally managed CDP sessions are
not supported by this probe. The interception remains until the context closes.
Other write-shaped authentication requests are blocked; an unverified portal is
reported honestly instead of assuming sign-in or granting account-write access.

Results are limited to fixed categories/stages, safe IDs, timestamps and validated
build hashes. Raw error strings, page text, snapshots, contact details, cookies
and tokens are not exposed by the diagnostics endpoints. Existing encrypted
booking dispatch records retain their original protection. A report distinguishes
connection loss, offline hosts, dispatch state/topology/timeout errors, busy
profiles, blocked websites and unverified/required sign-in.

## Deployment

Deploy the Cloud change through the normal Railway pipeline. Existing failed
dispatches become visible in Connections without rebuilding the host. Older
records may show unknown builds/stages because they did not capture them.

Then rebuild the enrolled host once to add the probe capability and report its
build revision. From its existing checkout, after the PR is merged:

```bash
cd ~/kyrex
git pull --ff-only
KYREX_HOST_BUILD_SHA="$(git rev-parse HEAD)" docker compose \
  --env-file browser-host/enrollment.env \
  -f browser-host/docker-compose.cloud.yml --profile cloud \
  up -d --build agent
```

The Compose profile volume and enrollment remain in place. A restart alone does
not install the new host code. A connected old agent receives an explicit update
required report instead of being called offline.

Open Connections → Browser. Select the Bot and the page used by the failing
workflow, then tap **Test browser connection** once. For the existing configured
Level 6 acceptance case, its adapter starts at
`https://level6training.com/book-a-class`; this is not a default for other users.
Use the saved report to locate the failed stage before a new booking preparation.
No reservation is submitted by this test.

## Recovery and API

POST `/api/browser-diagnostics` accepts only `bot_id`, `url` and `request_id`.
The owner is server-derived. A repeated request ID observes the same attempt;
changed details are rejected. The check is admitted with a 75-second deadline,
and the host-channel wait is capped below that deadline. Expired pending work is
never dispatched. GETs only observe results; closing/reopening Connections does
not cancel, restart or replay the check. If the poller is down, an expired saved
check is displayed as a timeout without making a GET execute anything.

GET `/api/browser-diagnostics` projects recent owner-scoped checks/failures and
eligible Browser Bots. GET `/api/browser-diagnostics/{reference}` retrieves one
owner-owned report. Unknown and foreign references both return 404. Normal
booking failures now include their durable `bd-…` diagnostic reference; dispatch
errors are no longer all presented as an offline agent.

This feature diagnoses the existing browser path; it does not establish the cause
of a previously reported live failure or claim that a real booking has succeeded.
