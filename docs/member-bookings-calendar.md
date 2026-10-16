# Existing member bookings and Calendar

This adds a read/import workflow beside the existing Level 6 booking workflow.
It does not prepare, submit, cancel, reschedule, pay for, or repeat a workout
booking. The existing workout booking driver, schema and single-use submission
store are unchanged.

Ask a running owner Bot:

- “What have I booked at Level 6?”
- “Show my booked classes next week.”
- After the result: “Add those to my calendar.”
- Or: “Verify my Level 6 bookings and add them to my calendar.”

After a studio has been identified, an unqualified booking read covers the next
30 local calendar days. A new conversation asks which service to check rather
than assuming that every booking is a Level 6 class. Today,
tomorrow, this week and next week are supported. The reply identifies the
window. A read lists confirmed class bookings, not cancelled/waitlisted classes
or the public class schedule. An actual empty member response is distinct from
failed sign-in, unsupported data, or an incomplete response.

## Browser boundary

The enrolled owner Browser Bot and its existing private member profile are
selected through the same binding helper as booking. A new, separate host
adapter blocks account/booking HTTP writes, destructive GET paths and WebSockets;
service workers are blocked. It allows the existing exact public guest bootstrap
and the portal's native POST to `https://auth.glofox.com/refresh` with only a
`refresh_token` body field. This maintains the saved session without a new login;
it cannot book, cancel, pay, select a family member or change an account. The
request must originate in the supported studio portal, cannot be redirected,
and still requires the owner's Host site permission. Tokens stay on the Host.
It navigates only observed self-profile/My bookings controls, then observes the
portal's native member-specific GET. No user/model-supplied member identifier,
API URL, authorization header or JavaScript is accepted.

The adapter uses the documented [member bookings GET](https://api-portal.glofox.com/api-portal/glofox/api/openapi-reference.html)
`/2.0/bookings`, with the observed member authorization staying on the Browser
Host. Controlled GETs on that same endpoint replace native filters with the
bounded studio/time window and page through at most 400 records. Every returned
record must belong to that member and studio. Only validated class references,
titles and actual start/duration become Cloud evidence. Cookies, raw member IDs,
authorization headers and the full response are never returned to Cloud.

If a portal version does not expose this observed member read, the adapter
reports unavailable. It does not guess a member identity, use guest schedule
data as booking proof, search email instead, or attempt a booking.

Failures distinguish an explicitly signed-out portal from an unverified/loading
session, a missing/unsupported portal, a blocked session refresh and a missing
My bookings read. Only fixed error codes cross into Cloud; exception text,
network bodies and credentials are not copied into Chat. An older Host without
these codes still returns the existing generic verification failure.

## Calendar boundary and recovery

Reads and imports run on existing CloudTaskStore/worker/Flux tasks. Reopening a
conversation observes its saved task; it does not resubmit. A pronoun follow-up
uses the latest native result in that same owner/Bot/conversation and imports
exactly that selected list. Unrelated assistant results do not select bookings.

Import re-reads Glofox, checks the owner's selected Calendar for exact timed
duplicates, and displays the exact events/account/destination using the existing
Calendar approval protocol. All-day workout labels are separate events.
After approval it rechecks the member session, selected bookings, Browser Host,
Google connection, destination, permissions and calendar contents. Changed or
cancelled classes stop the import instead of changing an approved event.

Creates use the existing Calendar writer with an optional destination/connection
binding and deterministic Google event IDs per reservation/account/destination.
No provider error is automatically replayed. After a timeout, check Calendar and
ask again: the fresh duplicate read reconciles existing exact events, and a
repeated create uses the same provider ID. Edited imported events require manual
review. There are no updates or deletes in this import flow.

## Deployment and validation

Merge/deploy Cloud normally and rebuild the existing Browser Host agent so it
contains the new read adapter. Do not delete profiles or redo the working
booking setup. From the existing host checkout:

```sh
git pull --ff-only origin main
docker compose --env-file browser-host/enrollment.env -f browser-host/docker-compose.cloud.yml --profile cloud up -d --build agent
```

Controlled tests cover the actual Chat/worker/reopened follow-up, exact Calendar
review, duplicates, owner isolation, changed/revoked resources, uncertain writes,
real intercepted Chromium reads and blocked writes. The existing workout booking
regressions remain required. These checks do not establish live portal success:
after deployment, first run the read-only “what have I booked?” request, compare
it with My bookings, then review a Calendar import.
