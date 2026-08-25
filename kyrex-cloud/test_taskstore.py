"""Tests for taskstore.py — persistent task queue for K-Bot.

Covers: create/get/update/list_by_bot, the on-disk schema, the TASKSTORE_FILE
override, validation rejection, persistence across a reload, and concurrent
access (the module-level lock must keep the single JSON file uncorrupted).

Run: python3 test_taskstore.py
"""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import taskstore

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def _reset(path):
    """Point taskstore at *path* and delete any existing file."""
    taskstore.TASKSTORE_FILE = path
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# ── Schema under test (kept here as a guard against silent drift) ──────────
EXPECTED_KEYS = {
    "task_id", "bot_id", "repo_url", "task_text", "executor_prefix",
    "status", "workspace_ref", "result", "error", "created_at", "updated_at",
}


print("=== Setting up test environment ===")
tmpdir = tempfile.mkdtemp(prefix="taskstore_test_")
TASKS_FILE = os.path.join(tmpdir, "tasks.json")
_reset(TASKS_FILE)
print("Setup complete.\n")


# ── Test 1: create returns a complete queued record ────────────────────────
print("Test 1: create returns a complete queued record")
rec = taskstore.create(bot_id="bot-a", repo_url="https://x/repo.git",
                       task_text="do the thing", executor_prefix="repo")
check("record has all expected keys",
      EXPECTED_KEYS == set(rec.keys()), f"got {sorted(rec.keys())}")
check("task_id is a 32-char hex string",
      isinstance(rec["task_id"], str) and len(rec["task_id"]) == 32)
check("bot_id stored", rec["bot_id"] == "bot-a")
check("repo_url stored", rec["repo_url"] == "https://x/repo.git")
check("task_text stored", rec["task_text"] == "do the thing")
check("executor_prefix stored", rec["executor_prefix"] == "repo")
check("status is queued", rec["status"] == "queued")
check("workspace_ref defaults to None", rec["workspace_ref"] is None)
check("result defaults to None", rec["result"] is None)
check("error defaults to None", rec["error"] is None)
check("created_at is set", isinstance(rec["created_at"], str) and rec["created_at"])
check("updated_at equals created_at on create",
      rec["updated_at"] == rec["created_at"])
check("file was written", Path(taskstore.TASKSTORE_FILE).exists())


# ── Test 2: get returns the record; unknown id returns None ────────────────
print("\nTest 2: get returns the record; unknown id returns None")
got = taskstore.get(rec["task_id"])
check("get returns the stored record", got == rec)
check("get on unknown id returns None", taskstore.get("does-not-exist") is None)


# ── Test 3: update changes fields, bumps updated_at, returns record ─────────
print("\nTest 3: update changes fields and bumps updated_at")
upd = taskstore.update(rec["task_id"], status="running", workspace_ref="/ws/alpha")
check("status updated", upd["status"] == "running")
check("workspace_ref updated", upd["workspace_ref"] == "/ws/alpha")
check("updated_at bumped", upd["updated_at"] >= rec["updated_at"])
check("get reflects the update",
      taskstore.get(rec["task_id"])["status"] == "running")
check("update on unknown id returns None",
      taskstore.update("ghost", status="done") is None)


# ── Test 4: update rejects invalid status and unknown fields ───────────────
print("\nTest 4: update rejects invalid status and unknown fields")
try:
    taskstore.update(rec["task_id"], status="bogus")
    check("invalid status raises TaskStoreError", False, "no exception")
except taskstore.TaskStoreError:
    check("invalid status raises TaskStoreError", True)
try:
    taskstore.update(rec["task_id"], not_a_field="x")
    check("unknown field raises TaskStoreError", False, "no exception")
except taskstore.TaskStoreError:
    check("unknown field raises TaskStoreError", True)


# ── Test 5: list_by_bot filters by bot_id, oldest-first ────────────────────
print("\nTest 5: list_by_bot filters by bot_id, oldest-first")
_reset(os.path.join(tmpdir, "list.json"))
a = taskstore.create(bot_id="bot-b", repo_url="r", task_text="t1")
b = taskstore.create(bot_id="bot-b", repo_url="r", task_text="t2")
c = taskstore.create(bot_id="bot-c", repo_url="r", task_text="t3")
lst_b = taskstore.list_by_bot("bot-b")
check("two tasks for bot-b", len(lst_b) == 2, f"got {len(lst_b)}")
check("ordered oldest-first",
      [t["task_id"] for t in lst_b] == [a["task_id"], b["task_id"]],
      f"got {[t['task_id'] for t in lst_b]}")
check("bot-c task present",
      [t["task_id"] for t in taskstore.list_by_bot("bot-c")] == [c["task_id"]])
check("unknown bot returns empty list", taskstore.list_by_bot("ghost") == [])


# ── Test 6: create auto-generates id; duplicate id rejected ────────────────
print("\nTest 6: create auto-generates id; duplicate id rejected")
_reset(os.path.join(tmpdir, "gen.json"))
r1 = taskstore.create(bot_id="x", repo_url="r", task_text="t")
r2 = taskstore.create(bot_id="x", repo_url="r", task_text="t2")
check("auto-generated id is non-empty str", isinstance(r1["task_id"], str) and r1["task_id"])
check("ids are unique", r1["task_id"] != r2["task_id"])
try:
    taskstore.create(bot_id="x", repo_url="r", task_text="t", task_id=r1["task_id"])
    check("duplicate task_id raises TaskStoreError", False, "no exception")
except taskstore.TaskStoreError:
    check("duplicate task_id raises TaskStoreError", True)


# ── Test 7: update persists to disk (reload reflects it) ───────────────────
print("\nTest 7: update persists to disk")
_reset(os.path.join(tmpdir, "persist.json"))
prec = taskstore.create(bot_id="p", repo_url="r", task_text="t")
taskstore.update(prec["task_id"], status="done", result={"status": "ok"})
with open(taskstore.TASKSTORE_FILE) as f:
    data = json.load(f)
check("status persisted as done", data[prec["task_id"]]["status"] == "done")
check("result persisted", data[prec["task_id"]]["result"] == {"status": "ok"})


# ── Test 8: corrupt store file raises rather than looking empty ────────────
print("\nTest 8: corrupt store file raises TaskStoreError")
with tempfile.TemporaryDirectory() as td:
    cf = os.path.join(td, "corrupt.json")
    taskstore.TASKSTORE_FILE = cf
    with open(cf, "w") as f:
        f.write("{ not json")
    try:
        taskstore.get("anything")
        check("corrupt file raises TaskStoreError", False, "returned silently")
    except taskstore.TaskStoreError:
        check("corrupt file raises TaskStoreError", True)
_reset(TASKS_FILE)


# ── Test 9: concurrent create/update keeps the file uncorrupted ────────────
print("\nTest 9: concurrent create/update keeps the file uncorrupted")
_reset(os.path.join(tmpdir, "concurrent.json"))


def _worker_create(i):
    taskstore.create(bot_id=f"cbot{i % 4}", repo_url="r", task_text=f"t{i}")


threads = [threading.Thread(target=_worker_create, args=(i,)) for i in range(40)]
for t in threads:
    t.start()
for t in threads:
    t.join()

store = json.loads(Path(taskstore.TASKSTORE_FILE).read_text())
check("all 40 tasks written", len(store) == 40, f"got {len(store)}")
check("all task_ids unique", len(set(store)) == 40)

# Concurrent updates to the same task must not lose or corrupt the record.
single = taskstore.create(bot_id="u", repo_url="r", task_text="t")


def _worker_update(i):
    taskstore.update(single["task_id"], task_text=f"updated-{i}")


threads = [threading.Thread(target=_worker_update, args=(i,)) for i in range(20)]
for t in threads:
    t.start()
for t in threads:
    t.join()

final = taskstore.get(single["task_id"])
check("record survived concurrent updates",
      final is not None and final["task_id"] == single["task_id"])
check("final task_text is one of the written values",
      final["task_text"].startswith("updated-"))


# ── Cleanup ────────────────────────────────────────────────────────────────
print("\n=== Cleaning up ===")
import shutil
shutil.rmtree(tmpdir, ignore_errors=True)


# ── Summary ────────────────────────────────────────────────────────────────
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)
