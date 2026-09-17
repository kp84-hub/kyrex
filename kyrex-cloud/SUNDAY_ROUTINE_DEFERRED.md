# Sunday Routine — DEFERRED (not implemented)

**Status: deferred follow-up. The Sunday Routine does NOT exist today.**

This document is the single authoritative statement of that status. No module,
route, test, or UI string in this repository may claim otherwise.

## What the Sunday Routine will be

A saved, owner-scoped **Chief-of-Staff routine** that, in one run, composes
three capabilities end to end:

1. **Facebook OCR** — read the gym's Facebook post to recover the week's
   class schedule image/text.
2. **Glofox reader** — read the pinned **Level 6 Training** 8:30 AM
   Mon–Sat schedule (the `glofox: schedule` connector).
3. **Approval-gated Google Messages sender** — draft the summary and send it
   to Google Messages **only after the owner's explicit approval**.

The scheduled trigger is a **Sunday-evening** run. (The Glofox reader's week
window is already Sunday-aware: a Sunday run selects the *following* calendar
Monday–Saturday. Sunday itself is never part of a reported window — see
`glofox_api.py`.)

## Why it is deferred

The routine cannot be assembled until **all three** capabilities are
implemented. Current state:

| Capability | State |
| --- | --- |
| Glofox Level 6 reader | **Implemented.** `glofox_api.py` + the pinned `glofox: schedule` Chat command (`dev_bot.submit_glofox_task` → `serve.run_task(executor_prefix="glofox")`). |
| Facebook OCR | **Not implemented.** No OCR/Facebook reader exists in the tree. |
| Approval-gated Google Messages sender | **Not implemented as a sender.** The approval *boundary* exists (`messaging.py`) and Google connector *foundation* exists (`connectors.py`, read-only Gmail/Calendar), but `messaging.py` wires **no** provider — every delivery refuses — and `connectors.py` explicitly does not implement sending. |

Because the sender and the OCR reader are missing, there is no code path that
can produce a Sunday Routine run today.

## Scope boundary (deliberate)

- This change is scoped to the **Chat-accessible Level 6 Glofox reader** only.
- The Sunday composition is **NOT** wired into the untracked `routines.py`
  (or any other routine module). `routines.py` is out of scope for this work.
- No frontend preset, API route, scheduler entry, or test asserts a working
  Sunday Routine. If such an assertion is ever added before all three
  capabilities ship, it is a defect and must be removed or corrected.

## When it will be built

Only after **all three** of the following are implemented and independently
verified:

1. the Facebook OCR reader,
2. the Glofox Level 6 reader (done),
3. the approval-gated Google Messages sender.

At that point the Sunday Routine composes the three through the existing
durable task path with the owner's approval gating the send.
