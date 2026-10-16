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

Glofox session verification waits up to 12 seconds for its embedded member
portal and saved-session UI. A temporary login link during initialization is
not a completed sign-out check. Only an explicit login/sign-in UI remaining at
the deadline reports `login_required`; loading, ambiguous or unreadable UI
reports `session_unverified`. An intercepted member-authentication POST reports
`session_verification_blocked` instead of telling an already signed-in user to
log in again. Such requests remain blocked. The booking adapter uses the same
readiness observation before preparing controls; existing exact-review and
submission guards remain authoritative.

These host-side changes require rebuilding the **agent image** after Cloud is
deployed. The viewer image rebuild is separate. A passed controlled fixture
does not prove that a live Glofox session works under the agent runtime.

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

### Viewer-to-automation profile handoff

The viewer must stop Chrome before stopping its X display. Its shutdown now
gives the browser parent time to flush the profile, then terminates/reaps the
remaining child groups. Docker allows 70 seconds for that bounded shutdown.
While still holding the shared owner/Bot profile lock, the viewer removes only
singleton symlinks naming its own stopped Chrome hostname/PID, after proving
that its process group has no live members. Foreign or unreadable locks,
unexpected regular files and symlinked profile directories are left untouched.
Profile contents and cookies are never deleted by this cleanup.

This fix is in the **viewer image**, which is separate from the Cloud/agent
image. End an existing viewer before deploying, then rebuild it on the VPS:

```bash
cd ~/kyrex
python3 browser-host/viewer_ctl.py end
git pull --ff-only
KYREX_VIEWER_OWNER=unused KYREX_VIEWER_BOT=unused \
  KYREX_VIEWER_VNC_PASSWORD_FILE="$PWD/browser-host/viewer-vnc-pass" \
  docker compose -f browser-host/docker-compose.viewer.yml --profile viewer build viewer
```

The placeholder owner/Bot values only satisfy Compose validation during build;
they never start a viewer or select a profile. Start the viewer normally, sign
in, end the session with `viewer_ctl.py end`, and run the Connections test
again. Closing the noVNC browser tab does not end the viewer session.

The viewer never guesses whether an unknown lock from a previous container is
stale. If a prior image already left such a lock, a host administrator must
first confirm that no browser holds that exact profile (across all containers
and host processes), acquire the shared profile lock, and remove only its
stale singleton symlinks. Do not delete the profile or infer safety from an
absent PID in one container. Forced container/host death can still require
this recovery; the new teardown addresses orderly stop, TTL and child failure.

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

## Saved iframe sessions and Chrome storage

The manual viewer uses Chrome's normal third-party storage partitioning.
Some Playwright versions disable `ThirdPartyStoragePartitioning`, so an
automated browser can open the correct profile yet miss the login saved in
an embedded portal. Waiting longer does not repair that mismatch.

Managed local launches now discover the installed Playwright's default flags
with a disposable, bounded Chrome launch and supported CDP command-line query.
The probe does not open an owner profile or visit a website. The real managed
launch removes only that storage-partitioning opt-out, preserving the remaining
flags. A failed probe stops the launch instead of silently changing storage
semantics. Remote CDP guests and unmanaged sessions retain their launch behavior.
No profile data is migrated, copied between owners, or deleted.

Rollout requires rebuilding the agent image; the viewer image is unchanged.
The live read-only probe passed browser launch, website access, and member
sign-in when only this flag difference was corrected. This establishes login
readiness, not a completed booking. Regression fixtures cover diagnostic and
normal managed launches, reopening the saved profile, and isolation from other
owners and top-level sites.

## Workout preparation failures

Workout adapter exceptions now retain only a fixed phase, an allowlisted error
type, and a repository source basename/line. Replay errors can include the
one-based step number. `KYREX_WORKOUT_FAILURE:` records this structural evidence
in host stderr; the saved result carries the same `workout_failure` object.
Exception text, stack arguments/locals, browser URLs, selectors, page/account
text, headers and storage values are not included. Unknown errors show the
phase and source location in Chat; recognized control/terms failures use fixed
actionable messages. Errors after a possible submission still report an unknown
outcome and must not be retried automatically.

Rebuild the agent to enable this evidence. For a new failed attempt, retrieve
the bounded structural log:

```sh
docker logs --since=10m --tail=200 browser-host-agent-1 2>&1 | rg KYREX_WORKOUT_FAILURE
```

Older failures discarded their underlying exception and cannot be reconstructed
from this new logging.
