#!/usr/bin/env python3
"""Read-only: live workers + delegation link state for still-active tasks."""
from task_store import default_store

store = default_store()
print("live workers:", store.live_workers())
print("all workers:")
for w in store._conn.execute(
    "SELECT worker_id, last_seen, started_at FROM workers ORDER BY last_seen DESC LIMIT 10"
).fetchall():
    print("  ", w)

for tid in ("task-1790119157325-653390ae", "task-1790116832488-3f7169b7"):
    t = store.get(tid)
    d = store.get_delegation_for_task(tid)
    print(f"\n{tid}: status={t['status']} cancel_requested={t['cancel_requested']} "
          f"claimed_by={t['claimed_by']} session={t['session_key']}")
    if d:
        print(f"   delegation {d['delegation_id']}: status={d['status']} "
              f"error={d['error']!r} relayed_at={d['relayed_at']}")
    else:
        print("   no delegation")
