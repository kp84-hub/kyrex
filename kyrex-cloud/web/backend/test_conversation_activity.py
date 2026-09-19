#!/usr/bin/env python3
"""Focused test: the sidebar's DURABLE active-work descriptor.

`chat_service._conversation_activity` is the backend half of the Kyrex Chat
sidebar active-work line. It must be derived exclusively from the existing
durable store — a non-terminal delegation (Chief-of-Staff work) wins over a
non-terminal ordinary Bot task — and must return ``None`` (never a terminal
or another owner's row) once the work settles.

Run: python3 test_conversation_activity.py
"""
import os
import sys
import tempfile

# Isolate the durable store BEFORE importing the modules (paths are read at
# import time), exactly like kyrex-cloud/test_flux.py.
_TMP = tempfile.mkdtemp(prefix="kx_activity_")
os.environ["KYREX_DATA_DIR"] = _TMP

_HERE = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_HERE))  # kyrex-cloud/
sys.path.insert(0, _CLOUD)
sys.path.insert(0, _HERE)

from task_store import CloudTaskStore  # noqa: E402

import chat_service  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


store = CloudTaskStore()  # DB under the temp KYREX_DATA_DIR
# The descriptor reads through chat_service._task_store(); point it at ours.
chat_service._task_store = lambda: store

USER = "owner"

# ── 1. store: newest NON-TERMINAL task for a conversation ──────────────
tid = store.submit(
    session_key="calendar-reader",
    task_text="Read this week's calendar",
    bot_id="calendar-reader",
    chat_id=USER,
    resolve_bot=False,
    conversation_id="c1",
)
task = store.latest_active_task_for_conversation("c1")
check("active task found", task is not None and task["task_id"] == tid)
check("active task is queued", bool(task) and task["status"] == "queued")
store.set_status(tid, "done")
check(
    "terminal task is not active work",
    store.latest_active_task_for_conversation("c1") is None,
)
check(
    "blank conversation id -> None",
    store.latest_active_task_for_conversation("") is None,
)

# ── 2. descriptor: an ordinary Bot task ────────────────────────────────
tid2 = store.submit(
    session_key="calendar-reader",
    task_text="Read this week's calendar",
    bot_id="calendar-reader",
    chat_id=USER,
    resolve_bot=False,
    conversation_id="c2",
)
act = chat_service._conversation_activity(USER, "c2")
check("task descriptor kind", bool(act) and act["kind"] == "task", repr(act))
check("task descriptor status", bool(act) and act["status"] == "queued")
check(
    "task descriptor carries owner-typed text",
    bool(act) and act["text"] == "Read this week's calendar",
)
check("task descriptor task_id", bool(act) and act["task_id"] == tid2)
store.set_status(tid2, "done")
check(
    "terminal task -> None",
    chat_service._conversation_activity(USER, "c2") is None,
)

# ── 3. descriptor: a delegation (Chief-of-Staff work) ──────────────────
did = store.create_delegation(
    owner=USER,
    coordinator_bot_id="chief",
    target_bot_id="calendar-reader",
    task_text="Read this week's calendar",
    parent_conversation_id="c3",
    status="queued",
)
store.set_delegation_status(did, "running", task_id="task-deleg-1")
act3 = chat_service._conversation_activity(USER, "c3")
check("delegation descriptor kind", bool(act3) and act3["kind"] == "delegation", repr(act3))
check("delegation descriptor status", bool(act3) and act3["status"] == "running")
check(
    "delegation descriptor target",
    bool(act3) and act3["target_bot_id"] == "calendar-reader",
)
check(
    "delegation descriptor text",
    bool(act3) and act3["text"] == "Read this week's calendar",
)
store.set_delegation_status(did, "done")
check(
    "terminal delegation -> None",
    chat_service._conversation_activity(USER, "c3") is None,
)

# ── 4. delegation takes precedence over an ordinary task ───────────────
store.submit(
    session_key="calendar-reader",
    task_text="Read this week's calendar",
    bot_id="calendar-reader",
    chat_id=USER,
    resolve_bot=False,
    conversation_id="c3b",
)
did_nested = store.create_delegation(
    owner=USER,
    coordinator_bot_id="chief",
    target_bot_id="calendar-reader",
    task_text="calendar request",
    parent_conversation_id="c3b",
    status="queued",
)
act_nested = chat_service._conversation_activity(USER, "c3b")
check(
    "delegation wins over a sibling task",
    bool(act_nested) and act_nested["kind"] == "delegation",
    repr(act_nested),
)

# ── 5. the owner-typed text is capped ──────────────────────────────────
store.create_delegation(
    owner=USER,
    coordinator_bot_id="chief",
    target_bot_id="calendar-reader",
    task_text="x" * 500,
    parent_conversation_id="c4",
    status="queued",
)
act4 = chat_service._conversation_activity(USER, "c4")
check(
    "descriptor text is capped",
    bool(act4) and len(act4["text"]) == chat_service._ACTIVITY_TEXT_LIMIT,
    str(len(act4["text"]) if act4 else None),
)

# ── 6. nothing to show: empty, unknown, and other-owner rows ───────────
check("no work -> None", chat_service._conversation_activity(USER, "empty") is None)
store.create_delegation(
    owner="someone-else",
    coordinator_bot_id="chief",
    target_bot_id="calendar-reader",
    task_text="secret",
    parent_conversation_id="c5",
    status="queued",
)
check(
    "another owner's delegation is invisible",
    chat_service._conversation_activity(USER, "c5") is None,
)

print()
if failures:
    print("FAILED:", failures)
    sys.exit(1)
print("✓ conversation activity descriptor: durable-only derivation verified.")
