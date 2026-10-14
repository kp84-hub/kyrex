# Book your Level 6 workout

After deployment, ask ordinary Kyrex Chat, the Overwatcher, or your owned
Workout Bot: **“Book my workout tomorrow at 8:30 AM.”** Give a date and time;
Kyrex asks for whichever is missing. The first version serves Level 6 Training
Group Fitness Classes within the next 14 days, using the studio's actual
America/New_York schedule. Other class times are supported when an exact
Group Fitness Class is available; 8:30 is not silently assumed.

Kyrex checks the actual class, trainer and available places, inspects the
member portal, and previews its native booking request without submitting
it. It shows the full date/time, trainer and membership/credit terms. Reply
**yes** to that latest proposal to book that exact class once. Edits create
a new review. “Yes, but change it to…” does not approve the previous class.
“No” discards an unsubmitted proposal; it does not cancel an existing class.

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
result never automatically retries: check **My bookings** in Glofox first.
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
