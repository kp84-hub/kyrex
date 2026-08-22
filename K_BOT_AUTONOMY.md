# K-Bot Autonomy — Persistent Bots, Replaceable Runtimes

Status: draft. A layer on top of `K_BOT_DESIGN.md` (tiers, executor contract)
and `KX_SERVE_DESIGN.md` (headless host). Neither is replaced.

## The idea

A Bot is a **persistent environment**. The engine is its **runtime**, and the
runtime is disposable.

```
BOT #1
├── Identity          model, prompt, name
├── Policy            what it may do unattended
├── Credentials       scoped MCP servers
├── Rift              repository, workspace, artifacts   ← durable
└── Engine            LLM, tools, session                 ← replaceable
```

Engine dies → Rift survives → restart the runtime → the Bot continues.

This is not "the Bot lost its brain," it is "the Bot's runtime crashed."

## Why environment beats conversation history

An autonomous coding Bot should not have to remember *"I think I edited
foo.py."* It can look at foo.py. It can run `git status`, read the diff, check
the branch, re-run the tests, read the logs.

The environment is externalized memory, and unlike an LLM memory system it
cannot hallucinate. This is the same verification-first rule the rest of the
project runs on, applied to state: don't trust the recollection, check the
ground truth.

## What Rift gives you, and what it does not

Rift already provides copy-on-write isolation, change detection via
`git status --porcelain`, and merge-back. A Bot's workspace is that machinery
with one policy inverted.

**Rift lifecycle must be inverted for Bots.** Today a workspace is disposable
by design:

- `defer discardWorkspace(mgr, ws)` on clean exit
- `discardWorkspace` on every signal path
- `sweepStaleRifts` prunes anything older than `riftMaxAge` at startup

All three would destroy a Bot's world. A Bot's rift must be exempt from the
sweep and from discard. Nothing distinguishes them today — that distinction is
the first thing to build, and getting it wrong loses a Bot's entire state.

**Rift holds what the Bot changed. It does not hold what the Bot was doing.**
Files are not intent. The following need explicit, durable storage outside the
rift:

| State | Why it cannot be reconstructed |
|---|---|
| Queued tasks | Nothing on disk says what was asked |
| Current task status | A half-finished edit looks like a finished one |
| "What was I doing" checkpoint | The diff shows the result, not the goal |
| Model configuration | Which model, which settings |
| Policy / permissions | Must survive a restart or autonomy resets |
| Credentials reference | Which MCP servers this Bot may reach |
| Audit log | See below — this is the recovery record |

## Pending approvals must not survive a restart

If a T2 approval is outstanding when the runtime dies, the restarted runtime
must **not** resume it. Recovering into "I was about to delete something and
the operator may have said yes" is the worst available state: the approval and
the operation are no longer provably connected.

Rule: **approvals die with the runtime.** On restart, any in-flight approval is
discarded and the operation must be re-requested from the beginning. A Bot that
loses its runtime mid-approval has done nothing, and says so.

## The audit log stops being optional

Unattended Bots without a durable record of their actions is the one
combination worth refusing to build. "Engine: DEAD, Rift: ALIVE, restart and
continue" only works if something recorded what was in flight.

Minimum record per entry: timestamp, bot id, operation, derived tier, decision
(auto / approved / denied / timed out), and outcome. Append-only, outside the
rift, and durable across container restarts — which rules out Railway's
filesystem and makes this a storage decision, not just a logging one.

This is also the answer to "You as master, inspecting results." You cannot
inspect results you did not record.

## Policy-based autonomy

"No constant approval prompts" and "safe" reconcile only through
pre-authorization. The tier vocabulary already exists; policy is a per-Bot
binding of operations to tiers.

```
bot: refactorer
  fs:read     T0  within ~/projects/foo
  fs:write    T0  within ~/projects/foo
  fs:delete   T2  always
  repo:*      T1
  mail:*      denied
```

Three rules carried over from the OpenBot review, already in
`K_BOT_DESIGN.md`:

- The host derives the tier from the operation; a Bot's policy may not lower a
  tier the host derived. Policy grants autonomy, it does not reclassify danger.
- Deny beats allow, and default is deny.
- Gating must account for alternate paths to the same effect. A Bot with
  `fs:delete` denied but shell access is not gated.

**Ship policy in dry-run first.** Evaluate and log every decision without
enforcing, read the audit for a few days, then switch to enforce. A governance
feature nobody dares switch on is not a governance feature.

## Shared engine, not shared state

**Decision: one engine serving every Bot.** Bots are not running engines; Bots
are *clients* of the engine. The engine is shared compute, the Bots own their
state, and the rifts provide the durable environments.

```
                    KYREX CLOUD
                         |
                   ENGINE RUNTIME
                         |
        +----------------+----------------+
        |                |                |
      Bot A            Bot B            Bot C
      Rift A           Rift B           Rift C
      Config A         Config B         Config C
      Policy A         Policy B         Policy C
```

This is the router model, and it gives one natural home for model routing,
rate limiting, provider failover, MCP management, concurrency, telemetry, and
cost tracking — instead of six copies of each.

### The invariant

**No Bot-specific state may have a global fallback.**

This is not a style preference. `ConfigManager.__init__` silently falling back
to a shared global config cost days to find precisely because the fallback was
invisible: the wrong answer looked like a right one. A shared engine multiplies
those surfaces, so the fallback itself has to go.

When a Bot asks for its model, workspace, MCP servers, environment, config,
tool state, or credentials, the engine returns **that Bot's value or an
explicit error**. Never a global. Never a default that happens to work.

The engine must have no concept of a "current Bot." Everything is keyed to a
session explicitly, passed as an argument, never read from ambient state.

This is testable, and it should be tested the way cross-session approval
isolation already is in `test_session_isolation.py`: set up two Bots, remove
one's config, and assert the lookup errors rather than silently returning the
other's.

### Credentials are the hard boundary

Process isolation would give this for free; shared compute means rebuilding it
logically. Every tool call passes through one chokepoint:

```
execute_tool(session_id, tool, arguments)
    -> resolve Bot from session_id      (error if unknown)
    -> load Bot policy                  (error if absent)
    -> check credential capability      (deny by default)
    -> derive tier from the operation   (host derives, never the executor)
    -> authorize or gate
    -> execute
```

Capabilities are named and scoped per Bot:

```
Bot A -> github:repo-a
Bot B -> gmail:readonly
Bot C -> railway:project-c
```

The chokepoint is also where tier derivation already belongs, so this
consolidates the policy work rather than adding to it. There must be exactly
one path from a tool request to execution — a second path is an ungated door,
which is the alternate-paths rule from `K_BOT_DESIGN.md` applied to the engine
itself.

### What this costs

Honestly: a wedged engine stalls every Bot, so the watchdog moves to the engine
level and needs restart-with-session-recovery. And the isolation guarantees are
enforced by code that must be right, rather than by the operating system. Both
are accepted, not dismissed — they are the price of the router model, and the
invariant above is what makes the price payable.

## Lifecycle

```
start   → create or attach rift, start runtime, load policy
pause   → stop runtime, rift untouched, queued tasks preserved
resume  → start runtime, reattach rift, discard any in-flight approval
kill    → stop runtime, keep rift
destroy → stop runtime, delete rift  (explicit, T2, typed confirmation)
```

`destroy` is the only operation that removes a Bot's world, and it should be
as hard to trigger as any other irreversible act.

Supervision: a watchdog notices `Engine: DEAD, Rift: ALIVE` and restarts the
runtime. Restart must be rate-limited — a Bot that crashes on startup should
stop and report, not restart forever.

## Build order

1. **Bot identity and registry.** A config file listing Bots: name, model,
   rift path, policy. Cheap, and it makes everything else addressable.
2. **Rift lifecycle inversion.** Mark a workspace as persistent; exempt it from
   the sweep and from discard. Test that a Bot's rift survives both.
3. **Lifecycle commands.** start / pause / resume / kill, with the supervisor.
   The supervisor watches the engine, not each Bot: one runtime, and recovery
   means restarting it and reattaching every Bot's rift.
4. **Audit log.** Durable, append-only, outside the rift. Before any autonomy.
5. **Policy, in dry-run.** Evaluate and log, do not enforce.
6. **Policy, enforcing.** Only after reading real dry-run output.
7. **Scoped credentials.** Per-Bot capabilities enforced at `execute_tool`.

Running alongside all of it: **remove global fallbacks from Bot-scoped state**,
and add the isolation tests that prove a missing per-Bot value errors instead
of resolving to someone else's.

Steps 1–3 are plumbing. Step 4 is the prerequisite for everything after it.
Steps 5–7 are where mistakes become expensive, which is why they are last.

## Open questions

- **Where does durable state live?** Railway's filesystem is ephemeral, which
  rules it out for the audit log and the task queue. Local disk under
  `kx serve` is the obvious answer and makes the local deployment primary
  rather than a fallback.
- **What is a session key when a Bot has a chat?** Telegram forum topics
  (`message_thread_id`) map cleanly onto Bots, and per-session locking already
  keys on an opaque value.
- **Who is the bottleneck?** Six Bots queuing approvals to one operator is a
  new failure mode. Bots running T0-only unattended and queuing anything higher
  for later is the point of policy, but the queue needs a shape.
