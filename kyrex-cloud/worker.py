#!/usr/bin/env python3
"""worker.py — Kyrex Cloud task-worker bootstrap (Milestone 1 production entry).

This is the minimal production entry point that wires the #61 foundation
(``CloudTaskStore`` + ``TaskWorker``) into the live service:

  * constructs a single ``CloudTaskStore`` (SQLite under ``DATA_DIR`` so it
    survives restarts),
  * constructs a ``TaskWorker`` whose notifier delivers operator messages to
    Telegram for Telegram-sourced tasks and records events otherwise,
  * recovers any tasks orphaned by a previous (dead) worker/process, and
  * starts the worker claim loop so queued tasks are discovered and executed
    automatically — no caller has to call ``claim_and_execute_once``.

Telegram and the Web API are *submitters*: they create tasks in the store and
the worker is the only thing that executes them.  This keeps a single
execution path (``serve.run_task`` driven by ``TaskWorker``) and a single
source of truth for task state.

Run:
    python3 kyrex-cloud/worker.py
"""

import os
import sys
import threading
import time
import uuid

import serve
from task_store import CloudTaskStore, TaskWorker


def build_notifier():
    """Return (send, edit) callbacks that deliver to the real transport.

    For Telegram-sourced tasks the chat id is a numeric Telegram chat id, so
    the message is delivered to Telegram (progress, approval prompts, results).
    For any other session (e.g. the web UI, where ``chat_id`` is a username)
    the worker returns a synthetic message id — the in-memory approval
    protocol still waits for a reply, but the reply is routed via the Cloud
    API ``store.respond()`` rather than a Telegram chat.
    """
    try:
        import telegram_bot
        tg_send = telegram_bot.send_message
        tg_edit = telegram_bot.edit_message
    except Exception:
        tg_send = None
        tg_edit = None

    def send(chat_id, text):
        if tg_send is not None and str(chat_id).isdigit():
            return tg_send(chat_id, text)
        return f"msg-{uuid.uuid4().hex[:12]}"

    def edit(chat_id, msg_id, text):
        if tg_edit is not None and str(chat_id).isdigit():
            tg_edit(chat_id, msg_id, text)

    return send, edit


def build_store(db_path=None) -> CloudTaskStore:
    return CloudTaskStore(db_path)


def build_worker(store: CloudTaskStore, worker_id=None, with_telegram=True) -> TaskWorker:
    send, edit = (None, None)
    if with_telegram:
        send, edit = build_notifier()
    return TaskWorker(store, worker_id=worker_id, send=send, edit=edit)


def start_telegram_loop():
    """Run the Telegram long-poll loop (submitter + reply receiver) in-thread."""
    import telegram_bot
    telegram_bot.main()


def main():
    # Write MCP servers config from env before any executor runs.
    serve.write_mcp_config()

    store = build_store()
    worker = build_worker(store, with_telegram=True)

    # Restart-safe discovery: reclaim/recover tasks orphaned by a dead
    # worker or process before we start claiming new ones.
    try:
        recovered = store.recover_stale()
        if recovered:
            print(f"[worker] recovered {len(recovered)} orphaned task(s) on startup",
                  flush=True)
    except Exception as exc:
        print(f"[worker] startup recovery error: {exc}", file=sys.stderr)

    worker.start()
    print(f"[worker] CloudTaskStore + TaskWorker started (worker_id={worker.worker_id})",
          flush=True)

    # In production the same container also runs the Telegram bot, which
    # submits tasks to this store and receives approval replies.  Running it
    # in-process keeps serve's in-memory pending-approvals shared with the
    # worker so Telegram replies resolve the right task.
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        tg = threading.Thread(target=start_telegram_loop, daemon=True,
                              name="telegram-loop")
        tg.start()
        print("[worker] Telegram poll loop started", flush=True)

    # Keep the process alive; the worker runs in daemon threads.
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("[worker] shutting down", flush=True)
        worker.stop()


if __name__ == "__main__":
    import sys
    main()
