"""Tests for bots.py — Bot registry persisted as JSON.

Covers: add and retrieve a Bot, duplicate id is refused,
get_bot on unknown id raises, remove leaves rift untouched,
and registry survives a save/load round trip.

Run: python3 test_bots.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bots

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


# ── Setup: redirect BOTS_FILE to a temp directory ──────────────────────
print("=== Setting up test environment ===")
tmpdir = Path(tempfile.mkdtemp(prefix="bots_test_"))
bots.BOTS_FILE = str(tmpdir / "bots.json")

# Create a fake rift directory that we can check survives removal.
rift_dir = tmpdir / "test_bot_workspace"
rift_dir.mkdir(parents=True)
rift_file = rift_dir / "hello.txt"
rift_file.write_text("I should outlive the removal of my Bot entry.\n")
assert rift_dir.exists(), "precondition: rift dir exists"

print("Setup complete.\n")


# ── Test 1: add a Bot and retrieve it with get_bot ─────────────────────
print("Test 1: add and retrieve a Bot")
created = bots.add_bot(
    bot_id="nightly-qa",
    name="Nightly QA Runner",
    model="anthropic:claude-sonnet-4-20250506",
    rift=str(rift_dir),
    policy={"allowed_commands": ["read"]},
    status="paused",
)
check("add_bot returns a bot dict", isinstance(created, dict))
check("returned bot has correct id",
      created.get("id") == "nightly-qa", f"got {created.get('id')!r}")
check("returned bot has correct name",
      created.get("name") == "Nightly QA Runner")
check("returned bot has correct model",
      created.get("model") == "anthropic:claude-sonnet-4-20250506")
check("returned bot has correct rift",
      created.get("rift") == str(rift_dir))
check("returned bot has correct policy",
      created.get("policy") == {"allowed_commands": ["read"]})
check("returned bot has correct status",
      created.get("status") == "paused")

retrieved = bots.get_bot("nightly-qa")
check("get_bot returns same dict", retrieved == created)
check("registry file was written",
      Path(bots.BOTS_FILE).exists())


# ── Test 2: adding a duplicate id is refused ───────────────────────────
print("\nTest 2: adding a duplicate id is refused")
try:
    bots.add_bot(
        bot_id="nightly-qa",
        name="Duplicate",
        model="some-model",
        rift="/tmp/other",
        status="stopped",
    )
    check("duplicate add raised ValueError", False, "no exception raised")
except ValueError as e:
    check("duplicate add raised ValueError", True)
    check("error message mentions the id",
          "nightly-qa" in str(e), f"msg={e!r}")
except Exception as e:
    check("duplicate add raised ValueError",
          False, f"raised {type(e).__name__}: {e}")


# ── Test 3: get_bot on an unknown id raises KeyError ───────────────────
print("\nTest 3: get_bot on unknown id raises KeyError")
try:
    bots.get_bot("nonexistent-id")
    check("unknown id raises KeyError", False, "no exception raised")
except KeyError as e:
    check("unknown id raises KeyError", True)
    check("error message is clear",
          "nonexistent-id" in str(e), f"msg={e!r}")
except Exception as e:
    check("unknown id raises KeyError",
          False, f"raised {type(e).__name__}: {e}")


# ── Test 4: removing a Bot leaves its rift path untouched on disk ──────
print("\nTest 4: removing a Bot leaves its rift path untouched on disk")
removed = bots.remove_bot("nightly-qa")
check("remove_bot returns the removed dict",
      removed.get("id") == "nightly-qa")
check("rift directory still exists",
      rift_dir.exists(), f"{rift_dir} was removed")
check("file inside rift still exists",
      rift_file.exists(), f"{rift_file} was removed")
check("file content unchanged",
      rift_file.read_text() == "I should outlive the removal of my Bot entry.\n")
check("bot is no longer in registry",
      "nightly-qa" not in bots.load_bots())

# Verify that get_bot now raises for the removed id.
try:
    bots.get_bot("nightly-qa")
    check("get_bot after removal raises KeyError", False, "no exception")
except KeyError:
    check("get_bot after removal raises KeyError", True)


# ── Test 5: registry survives a save/load round trip ───────────────────
print("\nTest 5: registry survives save/load round trip")
bots2_dir = tmpdir / "roundtrip"
bots2_dir.mkdir(parents=True)
bots2_rift = bots2_dir / "roundtrip_workspace"
bots2_rift.mkdir()

bots.BOTS_FILE = str(bots2_dir / "bots.json")

# Add multiple bots.
a = bots.add_bot(
    bot_id="bot-alpha",
    name="Alpha",
    model="openai:gpt-4o",
    rift=str(bots2_rift / "alpha"),
    status="running",
)
b = bots.add_bot(
    bot_id="bot-beta",
    name="Beta",
    model="anthropic:claude-opus-4",
    rift=str(bots2_rift / "beta"),
    policy={"timeout": 120},
    status="stopped",
)

# Simulate a fresh load by reading the JSON file directly.
with open(bots.BOTS_FILE) as f:
    raw = json.load(f)
check("file contains 2 bots", len(raw) == 2, f"got {len(raw)}")
check("bot-alpha present", "bot-alpha" in raw)
check("bot-beta present", "bot-beta" in raw)

# Reload through the module.
reloaded = bots.load_bots()
check("reloaded dict has 2 items", len(reloaded) == 2, f"got {len(reloaded)}")
check("alpha matches after round trip",
      reloaded["bot-alpha"] == a)
check("beta matches after round trip",
      reloaded["bot-beta"] == b)

# get_bot works after reload.
got = bots.get_bot("bot-alpha")
check("get_bot after round trip works", got == a)


# ── Test 6: add_bot without policy defaults to empty dict ──────────────
print("\nTest 6: add_bot defaults policy to empty dict")
c = bots.add_bot(
    bot_id="bot-gamma",
    name="Gamma",
    model="test:model",
    rift=str(bots2_rift / "gamma"),
    status="stopped",
)
check("policy defaults to empty dict",
      c.get("policy") == {}, f"got {c.get('policy')!r}")


# ── Test 7: invalid status raises ValueError ───────────────────────────
print("\nTest 7: invalid status raises ValueError")
try:
    bots.add_bot(
        bot_id="bad-status",
        name="Bad",
        model="test:model",
        rift="/tmp/void",
        status="invalid_status",
    )
    check("invalid status raises ValueError", False, "no exception")
except ValueError as e:
    check("invalid status raises ValueError", True)
    check("error message mentions valid statuses",
          "stopped" in str(e) and "running" in str(e) and "paused" in str(e),
          f"msg={e!r}")


# ── Cleanup ────────────────────────────────────────────────────────────────────

print("\nTest 8: a corrupt registry raises rather than looking empty")
with tempfile.TemporaryDirectory() as td:
    bots.BOTS_FILE = os.path.join(td, "bots.json")
    with open(bots.BOTS_FILE, "w") as f:
        f.write("{ not json")
    try:
        got = bots.load_bots()
        check("corrupt file raises", False,
              "returned %r - the Bots would look deleted, and the next "
              "save_bots would overwrite the damaged file" % (got,))
    except bots.RegistryError as exc:
        check("corrupt file raises RegistryError", True)
        check("error names the file", bots.BOTS_FILE in str(exc))

print("\nTest 9: a malformed entry rejects the file, not just the entry")
with tempfile.TemporaryDirectory() as td:
    bots.BOTS_FILE = os.path.join(td, "bots.json")
    with open(bots.BOTS_FILE, "w") as f:
        json.dump({
            "good": {"id": "good", "name": "G", "model": "m",
                     "rift": "/tmp/x", "policy": {}, "status": "stopped"},
            "bad": {"id": "bad", "status": "nonsense"},
        }, f)
    try:
        got = bots.load_bots()
        check("malformed entry raises", False,
              "returned %r - the bad Bot silently vanished" % (got,))
    except bots.RegistryError as exc:
        check("malformed entry raises RegistryError", True)
        check("error names the offending id", "bad" in str(exc))


# ─── Lifecycle: lest_status ──────────────────────────────────────────
#
# These tests verify the new lifecycle API.  Each uses a dedicated temp
# directory so they don't interfere with each other or with earlier tests.

print("\nTest 10: set_status persists across a reload")


def _reset_bots_file(td: str) -> dict:
    """Point bots.BOTS_FILE to a fresh registry in *td* and add a bot."""
    bots.BOTS_FILE = os.path.join(td, "bots.json")
    return bots.add_bot(
        bot_id="lifecycle-test",
        name="Lifecycle",
        model="test:model",
        rift="/tmp/lifecycle",
        status="stopped",
    )


with tempfile.TemporaryDirectory() as td:
    bot = _reset_bots_file(td)
    updated = bots.set_status("lifecycle-test", "running")
    check("set_status returns updated dict",
          updated["status"] == "running", f"got {updated['status']!r}")
    check("returned dict is the same object as stored",
          updated["id"] == "lifecycle-test")

    # Reload from disk and verify the change stuck.
    reloaded = bots.load_bots()
    check("status is running after reload",
          reloaded["lifecycle-test"]["status"] == "running",
          f"got {reloaded['lifecycle-test']['status']!r}")


print("\nTest 11: invalid status is refused and previous status survives on disk")
with tempfile.TemporaryDirectory() as td:
    _reset_bots_file(td)
    try:
        bots.set_status("lifecycle-test", "invalid_status")
        check("invalid status raises ValueError", False, "no exception")
    except ValueError:
        check("invalid status raises ValueError", True)

    # The status on disk must still be "stopped", not partially written.
    reloaded = bots.load_bots()
    check("previous status survives on disk",
          reloaded["lifecycle-test"]["status"] == "stopped",
          f"got {reloaded['lifecycle-test']['status']!r}")


print("\nTest 12: set_status on unknown id raises KeyError")
with tempfile.TemporaryDirectory() as td:
    _reset_bots_file(td)
    try:
        bots.set_status("nonexistent-id", "paused")
        check("unknown id raises KeyError", False, "no exception")
    except KeyError as e:
        check("unknown id raises KeyError", True)
        check("error mentions the id",
              "nonexistent-id" in str(e), f"msg={e!r}")


print("\nTest 13: created_at is set on add and unchanged by set_status")
with tempfile.TemporaryDirectory() as td:
    bot = _reset_bots_file(td)
    check("created_at present on add", "created_at" in bot,
          f"keys={list(bot.keys())}")
    check("created_at is a non-empty string",
          isinstance(bot["created_at"], str) and len(bot["created_at"]) > 0)

    created_at = bot["created_at"]
    bots.set_status("lifecycle-test", "paused")
    fetched = bots.get_bot("lifecycle-test")
    check("created_at unchanged after set_status",
          fetched["created_at"] == created_at,
          f"expected {created_at!r}, got {fetched['created_at']!r}")


print("\nTest 14: list_bots returns bots sorted by id")
with tempfile.TemporaryDirectory() as td:
    bots.BOTS_FILE = os.path.join(td, "bots.json")
    c = bots.add_bot("charlie", "Charlie", "m", "/tmp/c", status="stopped")
    a = bots.add_bot("alpha", "Alpha", "m", "/tmp/a", status="stopped")
    b = bots.add_bot("bravo", "Bravo", "m", "/tmp/b", status="stopped")

    sorted_bots = bots.list_bots()
    check("list_bots returns list",
          isinstance(sorted_bots, list))
    check("list_bots has 3 items",
          len(sorted_bots) == 3, f"got {len(sorted_bots)}")
    ids = [bot["id"] for bot in sorted_bots]
    check("bots sorted by id",
          ids == ["alpha", "bravo", "charlie"],
          f"got order {ids!r}")



print("\nTest 15: a registry written before created_at existed still loads")
with tempfile.TemporaryDirectory() as td:
    bots.BOTS_FILE = os.path.join(td, "bots.json")
    with open(bots.BOTS_FILE, "w") as f:
        json.dump({"legacy": {"id": "legacy", "name": "L", "model": "m",
                              "rift": os.path.join(td, "r"), "policy": {},
                              "status": "stopped"}}, f)
    try:
        loaded = bots.load_bots()
        check("legacy registry loads", "legacy" in loaded,
              "adding a required field must not orphan existing registries")
        check("created_at backfilled",
              loaded["legacy"].get("created_at") == "")
    except Exception as exc:
        check("legacy registry loads", False,
              "raised %s: %s" % (type(exc).__name__, exc))

print("\nTest 16: backfill does not excuse a genuinely missing field")
with tempfile.TemporaryDirectory() as td:
    bots.BOTS_FILE = os.path.join(td, "bots.json")
    with open(bots.BOTS_FILE, "w") as f:
        json.dump({"broken": {"id": "broken", "name": "B",
                              "policy": {}, "status": "stopped"}}, f)
    try:
        bots.load_bots()
        check("missing model/rift still rejected", False,
              "backfill loosened validation for fields that matter")
    except bots.RegistryError:
        check("missing model/rift still rejected", True)
print("\n=== Cleaning up ===")
shutil.rmtree(tmpdir, ignore_errors=True)


# ── Summary ────────────────────────────────────────────────────────────
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)