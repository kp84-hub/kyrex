# Pasted workout weeks and Calendar review

A user can paste a complete `#L6Workout` / `Level 6 — Workout Week` list after
“Can you add these to my calendar”. The bounded adapter accepts six dated
Monday–Saturday workout rows with trainer labels, including phone copies that
flatten line breaks. It preserves the supplied names and ISO dates, validates
weekday/date agreement and consecutiveness, and prepares the established
`Level 6 Workout: …` all-day events. It does not invent class times or register
a place at a gym. An exact event review shows all-day status before approval.
Incomplete, conflicting, malformed or timed rows receive a focused validation
message without submitting work or calling a model.

The pasted list is owner-provided evidence. It does not acquire Facebook/Glofox
verification by passing through the adapter. The approval explicitly labels the
source as not independently verified and shows the calendar destination and
exact event dates/titles. This path does not require or read a Browser Host.
The existing source-based weekly command and contextual follow-ups retain their
Facebook/Glofox source read and existing restrictions.

Both paths use CloudTaskStore, the owner's running unified Calendar Bot, the
existing Calendar Writer and its event-write scope, complete duplicate reads,
and the existing durable approval UI. A pasted task contains only a source tag
and six validated rows, not credentials, calendar identifiers or browser actions.
The route validates that it matches the owner's exact current message. It takes
precedence over stale selected-email and single-event state. Request replay
reattaches to the accepted task; different pending weeks cannot silently replace
one another. Cross-owner and unavailable/restricted Bot requests remain blocked.

Before pasted writes, recheck the task's worker claim, cancellation, owner and
Bot policy, calendar destination and connection identity. Every create supplies
the reviewed account/destination binding to the writer. Exact existing events
are skipped; conflicting tagged events stop the batch. An uncertain provider
write stops further creation and instructs the owner to check Calendar before
retrying. It is not reported as a confirmed absence. No calendar event is written
until the existing explicit approval gate is accepted.

Controlled tests use fake Calendar responses and create receipts. They do not
write to a user's Calendar or establish live provider success.
