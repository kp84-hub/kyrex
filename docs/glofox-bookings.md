# Book your Level 6 workout

After deployment, ask ordinary Kyrex Chat, the Overwatcher, or your owned
Workout Bot: **“Book my workout tomorrow at 8:30 AM.”** Give a date and time;
Kyrex asks for whichever is missing. The first version serves Level 6 Training
Group Fitness Classes within the next 14 days, using the studio's actual
America/New_York schedule. Other class times are supported when an exact
Group Fitness Class is available; 8:30 is not silently assumed.

Conversational requests such as “Could you please book my workout?” use the
same flow. If an older conversation treated that request as a Calendar event,
“I mean book the class on the website” recovers recent details supplied by
the owner and prepares a fresh class review. Assistant guesses are not used
as booking details. A month/day uses the studio's current local year (or its
next occurrence across New Year); booking does not require a user-selected
duration. Explicit requests to put a workout on Calendar retain that route.

Kyrex checks the actual class, trainer and available places, inspects the
member portal, and previews its native booking request without submitting
it. It shows the full date/time, trainer and membership/credit terms. Reply
**yes** to that latest proposal to book that exact class once. Edits create
a new review. “Yes, but change it to…” does not approve the previous class.
“No” discards an unsubmitted proposal; it does not cancel an existing class.

Screen sleep, changing tabs, or losing the chat network connection closes only
the viewer. The server finishes the current booking check and saves its reply
in that same conversation. Reopen it to see the review or recorded result;
do not resend the request or repeat “yes” to recover it. Stop still cancels the
current operation cooperatively. A check is bounded to five minutes. After a
server restart, an abandoned preparation is reported as interrupted, while a
started submission with no saved receipt remains unknown and requires checking
Glofox My bookings. Neither transcript refresh nor reconnect submits a booking.
Older stuck preparation/submission records can be retired on transcript reads
after ten minutes of inactivity, with the same distinction between preparation
and an uncertain submission.

## Read the class trainer

Trainer questions such as “Check who is running the Level 6 Group Fitness Class
tomorrow at 8:30am” read the same exact-class Glofox adapter used before booking.
“Look up who is the trainer for tomorrow L6 class at 8:30am” uses the same read.
Trainer wording is recognized without requiring a particular opening verb.
Recent follow-ups retain owner-supplied class details, including when correcting
an earlier mistaken booking-versus-Calendar question. A missing-detail question
asks only for the missing date or time; a short reply continues that read.
Context is bounded to 30 minutes in the same conversation and ends at an
unrelated owner request. Assistant guesses are not used as class details.
Relative dates retain the original message's local date across midnight.

Provide a date and AM/PM time. The read joins the class to the verified trainer
roster and returns its source directly, without Calendar, email or web-search
fallback. A full class can still have its trainer checked; the booking path
continues to require a future, available place and its exact owner review.
An unavailable source produces an unverified result, never a guessed trainer.
This is a Cloud change; no Browser Host rebuild is required for trainer reads.

Browser research reports now share one thread per owner, parent conversation
and Browser Bot. Each task keeps its own result identity inside that thread,
including repeated deliveries and concurrent worker completions. Existing
per-lookup report chats remain in history; this does not delete them or combine
results across owners, parent conversations or different Browser Bots.

## One-time member sign-in

The public schedule reader does not sign in as you. Booking uses the existing
owner-isolated **Browser Bot** profile on its connected Browser Host.
Keep that Bot running and deploy the Browser Host update as described in
[browser bookings](browser-bookings.md).

Open your existing private Browser Host manual viewer for that Browser Bot,
visit https://level6training.com/book-a-class, and sign in with your own
Glofox member account. Close the manual viewer when finished, then repeat
your workout request. Signing in in an unrelated desktop tab or phone app
does not connect the Browser Host's profile.

For an already configured private viewer on the host, its existing command is:

```bash
python3 browser-host/viewer_ctl.py start --owner YOUR_KYREX_OWNER --bot browser-bot --ttl 2700
```

Use the same owner and Browser Bot that are enrolled in your Kyrex account.
The viewer's existing VNC password file and Tailscale access remain required;
see [the Browser Host manual viewer guide](../browser-host/README.md#phase-3--manual-viewer-tailscale-only-implemented-in-repo-not-yet-activated).
Automation stops while that viewer holds the profile. Your password stays
in the owner-operated browser; do not enter it in Kyrex Chat or Railway.

Zoho email sending authorization is not needed. No studio API key is needed
for this browser path. Kyrex does not extract a login token to issue its own
API calls, and does not ask for a studio administrator account.

## Confirmation and limits

Supported member requests use Glofox's documented **2.3 branch bookings**
endpoint with `model: event`, the exact verified class ID, zero guests,
and explicit `join_waiting_list: false`. Kyrex observes the portal's own
request and leaves its credentials in the browser. The review binds its
safe body and a hash of the member session; a different account or token
requires a fresh review. Legacy/unknown endpoint shapes stop with an adapter
message. This first release is fixture-tested; an authenticated check of
your studio's current portal is still required before claiming live readiness.

The portal must show the class is free, included in the member's membership,
or consumes a stated credit. Paid drop-ins, purchases, additional guests,
waitlist requests, CAPTCHA and automated sign-in are unsupported. If a
class becomes full, its trainer changes, or the browser page/account/request
changes, it stops. Studio membership/eligibility rules still apply.

A confirmed result requires Glofox's actual response to report `BOOKED` and
`confirmed: true` for the reviewed branch, event and timestamp, with a real
booking ID. A button click or success phrase alone never counts. An uncertain
receipt triggers one read-only My bookings check in the same Browser Host
profile. The read must retain the reviewed member credential identity and any
explicit member ID, and return a complete list with one explicitly confirmed,
non-waitlist booking for the exact event, start time and class. Its booking ID
becomes the confirmation reference. Read permission, site access, account
changes, missing/ambiguous data or browser loss leave the outcome unknown.
This check never submits another booking. Existing unknown records are not
automatically replayed or rewritten by this change.
If the submitting session cannot verify the result, Chat makes one additional
read-only verification dispatch in a fresh operator using the same saved
Browser Host profile. This also handles a lost submission response. The
single-use verification task retains the reviewed credential identity, member
ID when present, event, date and start time. It carries no replay steps or
submit control, and Cloud explicitly denies `browser:booking` for that task.
Only one exact, complete member-list match changes the saved result to
confirmed. An unavailable, denied or mismatched read leaves it unknown; it
never replays the original write. Deploy both Cloud and the Browser Host for
this verification stage; an older Host fails closed.
An uncertain result never automatically retries: check **My bookings** in Glofox first.
This change does not add cancellations or recurring/bulk bookings.

The existing guest schedule/trainer monitor remains read-only. Workout
reviews and their private browser dispatches, results and progress are
encrypted using the existing owner storage. Server-issued single-use tasks
and a per-profile durable host claim limit each proposal to one native
booking request. Generic research/delegated tasks cannot approve or mint it.

Provider references:

- [Glofox web portal](https://support.glofox.com/hc/en-us/articles/46467154523668-How-to-Use-the-Web-Portal)
- [Glofox API reference](https://api-portal.glofox.com/api-portal/glofox/api/openapi-reference.html)
- [Integration authentication restrictions](https://apidocs-plat.aws.glofox.com/flows/login/)
