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


# ── Cleanup ────────────────────────────────────────────────────────────
print("\n=== Cleaning up ===")
shutil.rmtree(tmpdir, ignore_errors=True)


# ── Summary ────────────────────────────────────────────────────────────
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)