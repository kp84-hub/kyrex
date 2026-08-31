# K-Bot — Capability Tiers & Executor Interface

Status: draft. Defines the approval model and executor contract for `kx serve`.

## Why tiers exist

Kyrex Cloud never needed an approval model. Git supplied one for free: the worst
outcome of a confused agent turn is a bad PR you decline to merge. The workflow
*was* the gate.

The moment an executor can send mail, delete files, or hit a payment API, that
gate is gone and nothing sits between a misparse and a real effect. Worse, the
operator is on a phone and not watching. Tiers rebuild the review step that git
gave us implicitly.

**Design rule:** tier is a property of the *operation*, not the executor. A
Gmail executor has read ops and destructive ops; it does not get one blanket
trust level.

---

## The three tiers

### T0 — Auto (no confirmation)

Reversible, non-observable-to-others, no side effects outside the session.

- Read a file, list a directory, grep
- Search mail, read a thread, list calendar events
- Fetch a URL, read a repo
- Any query whose failure mode is "wrong answer," not "wrong state"

Runs immediately. Reported in the result summary, not confirmed in advance.

### T1 — Confirm (inline y/n)

Mutations that are recoverable, cheap to undo, or scoped to a sandbox.

- Write/edit a file inside a rift workspace
- Apply a label, move to a folder, mark read
- Create a draft (not send)
- Open a PR, push to a non-default branch
- Create a calendar event

Bot posts the proposed action with its concrete target and waits. A plain
`y` / `n` reply is enough. Times out to **no** after 10 minutes — silence is
never consent.

### T2 — Typed confirmation (echo the target)

Irreversible, externally visible, or expensive to undo.

- Delete/trash mail, empty trash, permanently delete anything
- Send an email or message to a third party
- `git push --force`, delete a branch, delete a remote
- Any write to a production system (Chronic Cortex, Firestore, Render)
- Rotate/revoke a credential, spend money

Bot echoes the **exact target** and requires the operator to type a matching
token — not `y`. The token must be derived from the target so muscle-memory
approval is impossible:

```
⚠️  T2: TRASH 1,247 messages matching
    category:promotions older_than:6m
    (oldest 2019-03-11, newest 2026-02-02)

Reply exactly:  TRASH 1247
```

Times out to **no** after 10 minutes. Never batch T2 ops behind one
confirmation — one approval, one operation.

---

## Escalation rules

1. **Ambiguity escalates.** If the executor can't map a request to a specific
   tier, it uses the higher one. Never the lower.
2. **Volume escalates.** A T1 op over more than 50 targets becomes T2. Labeling
   3 emails is not labeling 3,000.
3. **Scope escalates.** Any op touching `kyrex-cloud/` or `~/.kyrex/` is T2
   regardless of its base tier — that's K-Bot's own code and config, and a bad
   self-edit removes the channel used to send the fix.
4. **Tiers never de-escalate at runtime.** No "you approved something similar
   earlier," no session-level "approve all." If that becomes necessary, it is a
   config change made outside a live session.

---

## Executor contract

An executor is any process invoked as a subprocess that speaks the existing
NDJSON-ish line protocol on **stdout only**:

```
KYREX_PROGRESS:{"step":"searching","matched":1247}
KYREX_APPROVAL:{"tier":2,"summary":"TRASH 1247 messages","token":"TRASH 1247","detail":"..."}
KYREX_RESULT_JSON:{"status":"ok","final_response":"...","actions":[...]}
```

Rules, inherited from hard-won engine discipline:

- **stdout is a protocol channel.** No bare `print()`. Diagnostics go to stderr;
  the host drains it on a separate pipe (see `telegram_bot.py`, fixed 2026-08-19).
- Exactly one `KYREX_RESULT_JSON:` line, last.
- `KYREX_APPROVAL:` blocks the executor until the host writes `APPROVED` or
  `DENIED` on stdin. The host enforces the timeout, not the executor — an
  executor cannot be trusted to time out its own approval request.
- The host derives an op's tier from the operation itself, not from what the
  executor claims. The executor's self-declared tier is a hint the host may
  raise, but never a value the host acts on unverified.
- When gating an operation the contract must account for alternate paths to the
  same effect. Gating a delete tool while a general shell tool can achieve the
  same result leaves the operation ungated — the gate is only as strong as the
  weakest tool that can reach the same outcome.

### Registration

```
executors:
  repo:  git_workflow.py      # existing, unchanged
  mail:  executors/gmail.py
  fs:    executors/files.py
  cal:   cal_executor.py
  flux:  flux_executor.py     # Flux event streams: read T0, post T1, send T2
```

Routing is by explicit prefix (`repo: fix the parser`, `mail: clear promos`),
not by classifier. An LLM deciding whether "clean this up" means the inbox or
the codebase is a failure mode with no recovery path. Prefix-less messages go to
a default executor set in config.

---

## Open questions

- **Session model.** Subprocess-per-task (crash containment, proven, watchdog
  already built) vs. persistent session (conversational context, but a wedged
  session takes K-Bot down). Leaning subprocess-first; fake continuity by
  passing prior turns into task text.
- **Audit log.** Every T1/T2 approval and its outcome should be appended
  somewhere durable. Railway's filesystem is ephemeral — needs a decision.
- **Multi-chat.** Current auth is a single `ALLOWED_CHAT_ID`. Fine for now;
  revisit only if a second operator is ever added.
