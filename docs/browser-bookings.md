# Conversational restaurant reservations

For Level 6 workout classes, use the separate [Glofox member booking flow](glofox-bookings.md).

Ask Kyrex or your Overwatcher to book a table. Give the restaurant/location,
date, preferred time and party size; provide the actual booking link if you
have it. For example, “Book a table at Example Grill in Raleigh on Friday at
8:30 PM for four.”

Kyrex collects missing details, reads the actual site and asks for contact
fields that the form needs. It can use your connected dedicated mailbox as
the contact email. You do not need Zoho sending authorization for a website
booking; that separate permission is only for outgoing email requests.

When the form is ready, Kyrex shows the restaurant, location, full date and
time (venue local time), party size, reservation name, phone/email and actual
booking site. Reply **yes** to that latest proposal to submit it. A different
time, a change to party size or any other edit creates a fresh review. A yes
embedded in “yes, but change it to…” does not approve the old reservation.
An unrelated answer does not approve an earlier proposal. Reviews expire
after ten minutes. “No” cancels an unsubmitted proposal.

The browser replays only the observed preparation steps, rechecks the exact
form and visible terms, and checks active owner/Browser Bot/host authority
again before its single submit click. If availability changes, it stops and
asks for a fresh proposal. Repeated confirmations, page reloads and crashes
never automatically repeat a submission. If the outcome is unknown, check
the business or confirmation email before explicitly starting another booking.

A successful browser action alone is not confirmation. Kyrex reports a
confirmed reservation only when the actual page has an explicit confirmation
heading matching the business, date, time, party size and reservation name.
A booking request acknowledgement stays pending until the business confirms
it. The source URL and actual reference are retained in the owner booking
record. It never invents a confirmation number.

## Deploy the Browser Host update

Deploy the Cloud code normally. The already enrolled Browser Host must also
be rebuilt to include the new fixed booking operation. From its Kyrex checkout:

```bash
git pull --ff-only
```

Then restart its agent using the same enrollment file and profiles:

```bash
docker compose --env-file browser-host/enrollment.env -f browser-host/docker-compose.cloud.yml --profile cloud up -d --build agent
```

This reuses the existing owner-isolated profile, host pairing and site
permissions. It does not need another mailbox, a Railway upgrade or new
OAuth variables. Keep your Browser Bot running and its Browser Host connected.
Both the Bot and Host site permissions still apply to the restaurant and any
booking provider it links to. A blocked provider requires adding that actual
site to your existing permissions; no site access is silently widened.

## Supported pages and access boundaries

The first version supports accessible, labelled reservation forms with
observed text/date/contact inputs, party/time dropdowns, booking links and
bounded availability/date-picker controls. It can split a supplied full name
into first/last name fields. Website-specific custom widgets may need an
adapter. Sign-in, CAPTCHA, payment/card requirements, fee terms, unsupported
required checkboxes or unclear booking details stop automation. It does not
bypass verification, enter account passwords, accept charges or join a
waitlist in place of a reservation. The page's actual blocker is reported;
preparation never claims a reservation was made.

The submission request must match the reviewed form action and method.
Custom JavaScript booking endpoints need a site-specific adapter; approval
does not authorize unrelated requests to other endpoints on the same site.

The research Browser Bot remains read-only for ordinary tasks. The fixed
booking operation is granted only to a single-use task issued by the native
owner workflow; generic model/browser tasks cannot mint that permission,
choose arbitrary selectors or approve submission. An owner reply is scoped
to the exact conversation, proposal revision and latest review message.
Browser policy/binding changes invalidate that authority. The manual-viewer
lock and host-side site checks continue to apply.

Booking details/plans and private dispatch text/results are encrypted in the
existing Cloud storage. The host keeps a minimal per-profile submission
journal containing only booking IDs and attempt timestamps. The Chat
transcript retains the details you supply and the proposal you review;
none of this reads your personal Gmail. The configured Chat/Bot model sees
only booking inputs and bounded observed page evidence for this workflow.
