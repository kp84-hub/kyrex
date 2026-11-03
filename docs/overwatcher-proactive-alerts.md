# Proactive updates through The Overwatcher

## Goal

Existing Bots should bring saved changes, failures and review requests to the
owner's ongoing coordinator conversation without waiting for another prompt.
This work improves delivery of existing observations. It does not add business
automation, new Bots, new source scanning or new account permissions.

## Execution plan

1. Extend the existing notification inbox with an optional coordinator binding.
   Keep the original conversation and task as the source of truth.
2. Route alerts in the existing notification observer to one running,
   owner-owned coordinator with the existing coordination grant. Reuse its
   canonical conversation. Ambiguous or unavailable destinations keep the
   original notification; never choose a Bot by its display name.
3. Project these saved alerts into coordinator Chat and its sidebar, with a
   source link. Keep approval controls in the original task/review. Projection
   must never submit work, invoke a model or change an approval.
4. Reconcile obsolete task approvals before routing or device delivery.
   Preserve notification identities, read state and delivery deduplication.
   Observe saved email drafts, booking proposals and phone send previews as
   well: these native reviews do not emit a CloudTask approval event. Read
   fixed metadata and original Chat references only; keep bodies and source
   decisions untouched. Expiration, revisions and handled states retire alerts.
5. Show a small in-app notice for newly arriving alerts. Initial history must
   not generate a burst of notices. Existing explicit Web Push consent remains.
6. Test owner isolation, concurrent/restarted observation, stopped/reassigned
   coordinators, source links, handled approvals, existing notifications and UI.

## Acceptance

- A direct Bot task needing approval and a saved scheduled change appear in
  the same existing coordinator chat with the correct source link.
- Repeated scans, concurrent observers and reloads produce one alert identity.
- A task moving beyond review retires its obsolete review notification.
- Source tasks, review IDs and permission checks remain unchanged.
- No coordinator, two eligible coordinators, a stopped Bot or a changed owner
  cannot redirect information to another account or silently create a Bot.
- Existing task/automation notification producers use the same inbox. Quiet
  scans that do not create an alert remain quiet. A workflow must still have
  a configured trigger/check to discover a change.

## Rollout

Deploy Cloud and Chat after checks pass. This observer/projection change does
not require a Browser Host rebuild. Device push requires the owner's existing
browser permission; without it, alerts remain available in Chat and its inbox.
Controlled tests establish routing and delivery behavior, not live provider
availability or receipt of an external device notification.

## Validation completed

The implementation passed 161 backend checks covering notifications, coordinator
routing, native review observation, Calendar suggestions, Bot conversations,
delegation, sessions, email and phone-send reviews. All 14 notification component
checks and the production Chat build passed, along with the existing durable
task lifecycle and Flux replay harnesses. Source/compile and whitespace checks
passed. A local Chromium download was unavailable, so UI behavior was checked
with the repository's DOM test harness. CI must pass on the published head
before release. No production source read, send, approval or device delivery
was performed during implementation.
