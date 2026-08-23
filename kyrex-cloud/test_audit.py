"""Tests for audit.py — append-only JSONL audit log.

Covers: round-trip write/read, newest-first ordering, bot-id filtering,
invalid decision raises and nothing is appended, malformed line raises
rather than being silently skipped, and concurrent appends from two
threads all land.

Run: python3 test_audit.py
"""

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


# ── Helpers ────────────────────────────────────────────────────────────


def _reset_audit_file(td: str) -> str:
    """Point audit.AUDIT_FILE to a fresh path under *td* and return it."""
    path = os.path.join(td, "audit.jsonl")
    audit.AUDIT_FILE = path
    return path


# ── Test 1: entry round-trips through write and read ───────────────────
print("Test 1: entry round-trips through write and read")
with tempfile.TemporaryDirectory() as td:
    _reset_audit_file(td)
    audit.log("bot-a", "run", "tier1", "approved", "completed", detail="ok")
    entries = audit.read_entries()
    check("read_entries returns list", isinstance(entries, list))
    check("one entry returned", len(entries) == 1, f"got {len(entries)}")
    e = entries[0]
    check("bot_id matches", e.get("bot_id") == "bot-a", f"got {e.get('bot_id')!r}")
    check("operation matches", e.get("operation") == "run")
    check("tier matches", e.get("tier") == "tier1")
    check("decision matches", e.get("decision") == "approved")
    check("outcome matches", e.get("outcome") == "completed")
    check("detail matches", e.get("detail") == "ok")
    check("timestamp is present", "timestamp" in e)
    check("timestamp ends with Z or +",
          e["timestamp"].endswith("+00:00") or e["timestamp"].endswith("Z"),
          f"got {e['timestamp']!r}")


# ── Test 2: entries come back newest first ──────────────────────────────
print("\nTest 2: entries come back newest first")
with tempfile.TemporaryDirectory() as td:
    _reset_audit_file(td)
    audit.log("bot-first", "op1", "t1", "auto", "ok")
    audit.log("bot-second", "op2", "t2", "approved", "ok")
    entries = audit.read_entries()
    check("two entries returned", len(entries) == 2, f"got {len(entries)}")
    check("newest is first",
          entries[0]["bot_id"] == "bot-second",
          f"first entry has bot_id={entries[0]['bot_id']!r}")
    check("oldest is last",
          entries[1]["bot_id"] == "bot-first",
          f"last entry has bot_id={entries[1]['bot_id']!r}")


# ── Test 3: filtering by bot_id works ──────────────────────────────────
print("\nTest 3: filtering by bot_id works")
with tempfile.TemporaryDirectory() as td:
    _reset_audit_file(td)
    audit.log("bot-alpha", "scan", "t1", "auto", "ok")
    audit.log("bot-beta", "deploy", "t2", "approved", "ok")
    audit.log("bot-alpha", "test", "t1", "denied", "no")
    all_entries = audit.read_entries()
    check("all three entries returned without filter",
          len(all_entries) == 3, f"got {len(all_entries)}")

    alpha = audit.read_entries(bot_id="bot-alpha")
    check("filtered returns 2 for bot-alpha",
          len(alpha) == 2, f"got {len(alpha)}")
    check("both filtered entries have correct bot_id",
          all(e["bot_id"] == "bot-alpha" for e in alpha))

    beta = audit.read_entries(bot_id="bot-beta")
    check("filtered returns 1 for bot-beta",
          len(beta) == 1, f"got {len(beta)}")
    check("beta entry has correct bot_id",
          beta[0]["bot_id"] == "bot-beta")

    gamma = audit.read_entries(bot_id="bot-gamma")
    check("filtered returns empty for unknown bot",
          gamma == [], f"got {gamma!r}")


# ── Test 4: invalid decision raises ValueError and appends nothing ─────
print("\nTest 4: invalid decision raises and appends nothing")
with tempfile.TemporaryDirectory() as td:
    path = _reset_audit_file(td)
    # Write a valid entry first so we can verify it survives.
    audit.log("bot-a", "op", "t1", "auto", "ok")
    check("valid entry written", os.path.getsize(path) > 0)

    try:
        audit.log("bot-a", "op", "t1", "nonsense_decision", "bad")
        check("invalid decision raised ValueError", False, "no exception")
    except ValueError as e:
        check("invalid decision raised ValueError", True)
        check("error lists valid decisions",
              "auto" in str(e) and "approved" in str(e) and "denied" in str(e) and "timeout" in str(e),
              f"msg={e!r}")

    # Count lines — should still be 1 (the original valid entry).
    with open(path) as f:
        lines = f.readlines()
    check("no lines appended after invalid decision",
          len([l for l in lines if l.strip()]) == 1,
          f"got {len(lines)} non-empty lines")


# ── Test 5: malformed line raises rather than being skipped ────────────
print("\nTest 5: malformed line raises rather than being skipped")
with tempfile.TemporaryDirectory() as td:
    path = _reset_audit_file(td)
    # Write a valid line followed by garbage.
    audit.log("bot-a", "op", "t1", "auto", "ok", detail="good entry")
    with open(path, "a") as f:
        f.write("this is not json\n")
    try:
        audit.read_entries()
        check("malformed line raises ValueError", False, "no exception")
    except ValueError as exc:
        check("malformed line raises ValueError", True)
        msg = str(exc)
        check("error mentions line number", "line 2" in msg,
              f"msg={msg!r}")
        check("error mentions file path", path in msg,
              f"msg={msg!r}")
        check("error mentions parse failure",
              "JSONDecodeError" in msg or "malformed" in msg.lower() or "Expecting" in msg,
              f"msg={msg!r}")

    # Also test that a non-dict JSON line is rejected.
    with tempfile.TemporaryDirectory() as td2:
        path2 = _reset_audit_file(td2)
        audit.log("bot-a", "op", "t1", "auto", "ok")
        with open(path2, "a") as f:
            f.write('"just a string"\n')
        try:
            audit.read_entries()
            check("non-dict JSON line raises ValueError", False, "no exception")
        except ValueError:
            check("non-dict JSON line raises ValueError", True)


# ── Test 6: concurrent appends from two threads all land ───────────────
print("\nTest 6: concurrent appends from two threads all land")
with tempfile.TemporaryDirectory() as td:
    path = _reset_audit_file(td)

    N = 50

    def writer(prefix: str):
        for i in range(N):
            audit.log(f"{prefix}-{i}", "concurrent", "t1", "auto", "ok")
            # tiny yield to encourage interleaving
            threading.Event().wait(0.0001)

    t1 = threading.Thread(target=writer, args=("alpha",))
    t2 = threading.Thread(target=writer, args=("beta",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    entries = audit.read_entries()
    total = len(entries)
    check("all concurrent entries present",
          total == 2 * N, f"expected {2 * N}, got {total}")

    alpha_count = sum(1 for e in entries if e["bot_id"].startswith("alpha"))
    beta_count = sum(1 for e in entries if e["bot_id"].startswith("beta"))
    check("alpha thread wrote all entries",
          alpha_count == N, f"got {alpha_count}")
    check("beta thread wrote all entries",
          beta_count == N, f"got {beta_count}")

    # Verify ordering: entries are newest-first overall.
    timestamps = [e["timestamp"] for e in entries]
    check("newest-first ordering maintained",
          all(timestamps[i] >= timestamps[i + 1] for i in range(len(timestamps) - 1)),
          "timestamps not in descending order")


# ── Summary ────────────────────────────────────────────────────────────
print()
if not failures:
    print("ALL TESTS PASSED")
else:
    print(f"{len(failures)} FAILURE(S): {failures}")

sys.exit(1 if failures else 0)