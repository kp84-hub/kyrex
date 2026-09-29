#!/usr/bin/env python3
"""Read-only check: report any task/delegation still in an active state."""
from task_store import default_store

store = default_store()
active = ("queued", "running", "awaiting_approval")

print("db:", store.db_path)
for status in active:
    tasks = store.list_tasks(status=status, limit=500)
    print(f"tasks {status}: {len(tasks)}")
    for t in tasks:
        print(f"    {t['task_id']} cancel_requested={t['cancel_requested']}")

for status in active:
    dels = store.list_delegations(status=status, limit=500)
    print(f"delegations {status}: {len(dels)}")
    for d in dels:
        print(f"    {d['delegation_id']} error={d['error']!r}")

print("counts by status:")
from collections import Counter  # noqa: E402
print(" ", Counter(t["status"] for t in store.list_tasks(limit=500)))
print(" ", Counter(d["status"] for d in store.list_delegations(limit=500)))
