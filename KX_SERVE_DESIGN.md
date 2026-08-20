# `kx serve` — Headless Engine Host

Status: draft. Builds on `K_BOT_DESIGN.md`, which defines the approval tiers
and executor contract this depends on.

## What it is

A long-running host process that accepts tasks and runs them against the Kyrex
engine, with no TUI attached. Kyrex Cloud is already ~80% of this: it holds a
message loop, dispatches to executors, enforces approval tiers, and reports
results. What it lacks is a transport-independent entry point.

`kx serve` is that entry point. Telegram becomes one client of it rather than
the thing that contains it.

## Session model: subprocess-per-task

**Decision: each task gets a fresh engine subprocess that exits when the task
completes.** No persistent session.

Rationale:

- It is the model Cloud already runs, and it demonstrably works. The watchdog,
  the stderr split, and the busy lock are all built around it.
- A crashed or wedged executor kills one task, not the host. A persistent
  session that wedges takes every future task down with it, and recovering
  means restarting the host — which, on a phone-operated bot, means being
  unable to send the fix.
- State that survives across tasks is state that can be silently corrupted.
  The engine's own history has two separate bugs of exactly this shape
  (`ConfigManager.__init__` discarding lane config, `MCPManager` starting
  empty), both invisible until something downstream behaved oddly.

The cost is no conversational memory: "actually, undo that" means nothing to a
fresh subprocess. Mitigation, if it turns out to matter: the host keeps the
last N task summaries per session key and prepends them to the task text. That
is a host-side feature, not an engine-side one, and can be added later without
changing the executor contract.

Revisit persistence only when a concrete task fails *because* of the missing
continuity — not preemptively.

## Concurrency

**One task at a time per session key.** The current global `busy_lock` becomes
a per-session lock.

This is not a performance decision, it is an approval-model constraint.
`handle_approval_reply` resolves a bare `y` by finding the single pending
approval for a chat. With two concurrent tasks both awaiting approval, a bare
reply is ambiguous and there is no safe default — approving the wrong
destructive operation is exactly the failure the tiers exist to prevent.

If parallelism is wanted later, the honest options are:
- Require every approval reply to be an explicit reply-to (no bare replies), or
- Scope sessions so that concurrent tasks live in different chats/topics.

Both are real designs. Neither should be bolted on without deciding first.

## Interface

Transport-independent. The host exposes:

```
submit(session_key, task_text) -> task_id
status(task_id)                -> queued | running | awaiting_approval | done | failed | truncated
events(task_id)                -> stream of progress / approval / result
respond(task_id, text)         -> routes an approval reply
cancel(task_id)
```

Telegram's adapter maps onto this directly: a message is `submit`, progress
edits come from `events`, an approval reply is `respond`, and `/status` is
`status`. Nothing in `telegram_bot.py`'s approval logic needs to change — it
moves.

## MCP configuration

The engine loads MCP servers from `~/.kyrex/mcp_servers.json` at startup. On
Railway that file does not exist and the filesystem is ephemeral, so the host
must write it before spawning any executor:

- Read a `MCP_SERVERS_JSON` environment variable (same pattern as
  `TELEGRAM_BOT_TOKEN` — secrets stay in the platform's env, never in the
  image or in git).
- Write it to `~/.kyrex/mcp_servers.json` at host startup, before the first
  task.
- If the variable is absent, start with zero servers and say so in the startup
  log rather than failing.

Do not bake a servers file into the Docker image. Credentials in an image are
credentials in every layer of that image.

## Build order

1. **Extract the host loop.** Move `run_task`, `launch`, `busy_lock`, and
   `pending_approvals` out of `telegram_bot.py` into a `serve.py` that knows
   nothing about Telegram. `telegram_bot.py` becomes an adapter that calls it.
   No behavior change; the existing test suites must pass unmodified.
2. **Per-session locking.** Replace the global `busy_lock` with a dict keyed by
   session. Key `pending_approvals` by session too.
3. **MCP config delivery.** Write `mcp_servers.json` from env at startup.
4. **`kx serve` entry point.** Wire the Go CLI to launch the host.
5. **First non-git executor.** Only after 1–4 are green.

Each step is one task. Step 1 is the largest and should be split if it exceeds
the engine's recursion budget.

## Open questions

- **Audit log.** Every T1/T2 approval and its outcome should be durable.
  Railway's filesystem is not. Needs a decision before any destructive
  executor ships.
- **Session key.** Telegram forum topics (`message_thread_id`) map cleanly onto
  sessions if multi-session is ever wanted. Unresolved, and step 2 should not
  hardcode "chat_id" in a way that forecloses it.
