#!/usr/bin/env python3
"""One-shot operator queue reset: cancel all active tasks and delegations.

Usage:  python3 reset_queue.py   (run from the kyrex-cloud directory)

Cancels every task in queued/running/awaiting_approval and marks every
delegation in those states cancelled, so the worker queue starts from a clean
slate.  Safe to re-run: already-terminal rows are ignored.
"""
from task_store import default_store

store = default_store()

active = ("queued", "running", "awaiting_approval")

print("store db:", store.db_path)
print("Cancelling tasks...")
for status in active:
    for task in store.list_tasks(status=status, limit=500):
        tid = task["task_id"]
        print(f"  {status}: {tid}")
        store.request_cancel(tid)

print("Cancelling delegations...")
for status in active:
    for d in store.list_delegations(status=status, limit=500):
        did = d["delegation_id"]
        print(f"  {status}: {did}")
        store.set_delegation_status(
            did,
            "cancelled",
            error="manual queue reset"
        )

print("Done.")
