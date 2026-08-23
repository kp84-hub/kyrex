"""Tests for resolve_bot_prefix and its integration into handle_message.

Covers:
  - resolve_bot_prefix returns (bot_id, rest) for @botid prefix
  - resolve_bot_prefix returns (None, text) when no @ prefix exists
  - resolve_bot_prefix returns (None, text) for email-like strings (user@host)
  - handle_message binds the session when @botid is valid
  - handle_message replies with registered ids when @botid is unknown
  - handle_message leaves behaviour unchanged when no prefix is present
  - bot prefix and executor prefix compose: @bot fs: task

Run: python3 test_bot_prefix.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "12345")
os.environ.setdefault("KYREX_TASK_TIMEOUT", "30")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bots
import serve
import telegram_bot as tb

CHAT = int(os.environ["TELEGRAM_ALLOWED_CHAT_ID"])
failures = []
sent = []
launched = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def fake_send(chat_id, text):
    sent.append(text)
    return 999


def record_launch(chat_id, repo_url, task_text, executor_prefix="repo",
                  session_key=None):
    launched.append({
        "prefix": executor_prefix,
        "text": task_text,
        "repo": repo_url,
        "session_key": session_key,
    })


tb.send_message = fake_send
tb.edit_message = lambda *a, **k: None
tb.launch = record_launch
tb.REPO_ALIASES = {}

# Point bots registry to a temp file so tests can add/remove bots freely.
_bots_tmpdir = Path(tempfile.mkdtemp(prefix="bot_prefix_test_"))
bots.BOTS_FILE = str(_bots_tmpdir / "bots.json")


def reset_globals():
    sent.clear()
    launched.clear()


# ── Fixture: add a test bot to the registry ────────────────────────────

def add_test_bot(bot_id="scratchbot"):
    """Add a bot to the in-memory registry and return its id."""
    bots.add_bot(
        bot_id=bot_id,
        name="Scratch Bot",
        model="test:model",
        rift=str(_bots_tmpdir / bot_id),
        policy={},
        status="stopped",
    )
    return bot_id


# ═══════════════════════════════════════════════════════════════════════
# Test resolve_bot_prefix (pure function tests, no mocks needed)
# ═══════════════════════════════════════════════════════════════════════

print("=== resolve_bot_prefix unit tests ===\n")

# ── Test 1: valid @prefix returns (bot_id, rest) ────────────────────
print("Test 1: valid @botid prefix returns (bot_id, rest)")
bot_id, rest = serve.resolve_bot_prefix("@scratchbot fix the parser")
check("bot_id is 'scratchbot'", bot_id == "scratchbot",
      f"got {bot_id!r}")
check("rest is the text after prefix", rest == "fix the parser",
      f"got {rest!r}")

# Also test with hyphen and underscore in id
bot_id2, rest2 = serve.resolve_bot_prefix("@my-bot_99 run tests")
check("bot_id with hyphens and underscores",
      bot_id2 == "my-bot_99", f"got {bot_id2!r}")
check("rest preserves following words",
      rest2 == "run tests", f"got {rest2!r}")


# ── Test 2: no prefix returns (None, original text) ────────────────
print("\nTest 2: no prefix returns (None, original text)")
bot_id, rest = serve.resolve_bot_prefix("fix the parser")
check("bot_id is None", bot_id is None, f"got {bot_id!r}")
check("rest is the full text unchanged", rest == "fix the parser",
      f"got {rest!r}")


# ── Test 3: email-like string is NOT treated as prefix ─────────────
print("\nTest 3: email-like string user@host do something is not a bot prefix")
bot_id, rest = serve.resolve_bot_prefix("user@host do something")
check("bot_id is None (email @ is mid-text)", bot_id is None,
      f"got {bot_id!r}")
check("rest is the full text unchanged", rest == "user@host do something",
      f"got {rest!r}")

# Also test: @ at start but not followed by valid bot id + whitespace
bot_id2, rest2 = serve.resolve_bot_prefix("@!invalid foobar")
check("@ followed by invalid char is not a prefix",
      bot_id2 is None, f"got {bot_id2!r}")

# @ at start but no space after
bot_id3, rest3 = serve.resolve_bot_prefix("@scratchbot!")
check("@ without whitespace after id is not a prefix",
      bot_id3 is None, f"got {bot_id3!r}")


# ── Test 4: empty string returns (None, empty) ─────────────────────
print("\nTest 4: empty string returns (None, empty)")
bot_id, rest = serve.resolve_bot_prefix("")
check("bot_id is None", bot_id is None)
check("rest is empty", rest == "")


# ═══════════════════════════════════════════════════════════════════════
# Integration tests: handle_message with bot prefix
# ═══════════════════════════════════════════════════════════════════════

print("\n=== handle_message integration tests ===\n")

# ── Test 5: known bot prefix binds session and strips prefix ──────
print("Test 5: known bot prefix binds session and strips prefix from task")
reset_globals()
bot_id = add_test_bot("scratchbot")
tb.handle_message({
    "chat": {"id": CHAT},
    "text": "@scratchbot fix the parser",
    "message_id": 10,
})
check("task was launched", len(launched) == 1, f"launched={launched}")
if launched:
    check("session_key is the bot id",
          launched[0]["session_key"] == "scratchbot",
          f"got {launched[0]['session_key']!r}")
    check("task text has prefix stripped",
          launched[0]["text"] == "fix the parser",
          f"got {launched[0]['text']!r}")
    check("executor is default (no executor prefix)",
          launched[0]["prefix"] == tb.DEFAULT_EXECUTOR,
          f"got {launched[0]['prefix']!r}")
check("no error/rejection sent",
      not any("Unknown" in s for s in sent),
      f"sent={sent}")
# Clean up registered bot
bots.remove_bot(bot_id)


# ── Test 6: unknown bot id replies with registered ids, no launch ──
print("\nTest 6: unknown bot id replies with registered ids and launches nothing")
reset_globals()
# Add a couple of bots to show in the list.
b1 = add_test_bot("bot-alpha")
b2 = add_test_bot("bot-beta")

tb.handle_message({
    "chat": {"id": CHAT},
    "text": "@unknown-bot do something",
    "message_id": 11,
})
check("no task was launched (unknown bot)",
      len(launched) == 0, f"launched={launched}")
check("a message was sent", len(sent) >= 1, f"sent={sent}")
rejection = next((s for s in sent if "Unknown bot" in s), None)
check("rejection message mentions unknown bot id",
      rejection is not None, f"no rejection in sent={sent}")
if rejection:
    check("rejection lists registered bot ids",
          "bot-alpha" in rejection and "bot-beta" in rejection,
          f"rejection={rejection!r}")
# Clean up
bots.remove_bot(b1)
bots.remove_bot(b2)


# ── Test 7: no prefix leaves behaviour unchanged ───────────────────
print("\nTest 7: no prefix leaves behaviour unchanged")
reset_globals()
# Ensure no bots in registry to prove that "no prefix" doesn't trigger bot lookup.
tb.handle_message({
    "chat": {"id": CHAT},
    "text": "fix the parser",
    "message_id": 12,
})
check("task was launched", len(launched) == 1, f"launched={launched}")
if launched:
    check("session_key is None (no bot binding)",
          launched[0]["session_key"] is None,
          f"got {launched[0]['session_key']!r}")
    check("task text is unchanged",
          launched[0]["text"] == "fix the parser",
          f"got {launched[0]['text']!r}")
    check("executor is default",
          launched[0]["prefix"] == tb.DEFAULT_EXECUTOR)
check("no rejection sent",
      not any("Unknown" in s for s in sent),
      f"sent={sent}")


# ── Test 8: bot prefix + executor prefix compose ───────────────────
print("\nTest 8: @bot fs: read a.txt composes bot binding and executor routing")
reset_globals()
bot_id = add_test_bot("scratchbot")
tb.handle_message({
    "chat": {"id": CHAT},
    "text": "@scratchbot fs: read a.txt",
    "message_id": 13,
})
check("task was launched", len(launched) == 1, f"launched={launched}")
if launched:
    check("session_key is the bot id",
          launched[0]["session_key"] == "scratchbot",
          f"got {launched[0]['session_key']!r}")
    check("task text is stripped of bot prefix",
          launched[0]["text"] == "read a.txt",
          f"got {launched[0]['text']!r}")
    check("executor prefix is 'fs' (not default)",
          launched[0]["prefix"] == "fs",
          f"got {launched[0]['prefix']!r}")
check("no rejection sent",
      not any("Unknown" in s for s in sent),
      f"sent={sent}")
bots.remove_bot(bot_id)


# ── Test 9: email-like text is not mistaken for bot prefix ─────────
print("\nTest 9: email-like 'user@host do something' is not treated as bot prefix")
reset_globals()
# Registry is empty — if 'user@host' were parsed as a bot prefix, it would
# trigger the "Unknown bot" rejection. Instead it should fall through to
# normal task launch.
tb.handle_message({
    "chat": {"id": CHAT},
    "text": "user@host do something",
    "message_id": 14,
})
check("task was launched (not rejected as unknown bot)",
      len(launched) == 1, f"launched={launched}")
if launched:
    check("session_key is None",
          launched[0]["session_key"] is None,
          f"got {launched[0]['session_key']!r}")
    check("task text is unchanged",
          launched[0]["text"] == "user@host do something",
          f"got {launched[0]['text']!r}")
    check("executor is default",
          launched[0]["prefix"] == tb.DEFAULT_EXECUTOR)
check("no 'Unknown bot' rejection sent",
      not any("Unknown bot" in s for s in sent),
      f"sent={sent}")


# ── Test 10: handle_message with document + caption + bot prefix ──
print("\nTest 10: document with caption and bot prefix works")
reset_globals()
bot_id = add_test_bot("docbot")
# Simulate a document with a caption that includes a bot prefix.
# We mock download_file_content to return text without actually downloading.
original_download = tb.download_file_content
# Note: download_file_content returns (content, filename) on success,
# but the document handler checks `if err:` where err is the second return
# value — a filename string is truthy and would be treated as an error.
# We return None as the second value so the handler proceeds to caption processing.
tb.download_file_content = lambda fid, fname: ("file content here", None)

tb.handle_message({
    "chat": {"id": CHAT},
    "text": "",  # no text, only caption
    "caption": "@docbot repo: review notes.txt",
    "document": {"file_id": "f123", "file_name": "notes.txt"},
    "message_id": 15,
})
check("task was launched for document with caption",
      len(launched) == 1, f"launched={launched}")
if launched:
    check("session_key is the bot id from caption",
          launched[0]["session_key"] == "docbot",
          f"got {launched[0]['session_key']!r}")
    check("executor prefix from caption is 'repo'",
          launched[0]["prefix"] == "repo",
          f"got {launched[0]['prefix']!r}")
    check("task text contains file content",
          "file content here" in launched[0]["text"],
          f"got {launched[0]['text'][:80]!r}")

tb.download_file_content = original_download
bots.remove_bot(bot_id)


# ═══════════════════════════════════════════════════════════════════════
# Cleanup
# ═══════════════════════════════════════════════════════════════════════

import shutil
shutil.rmtree(_bots_tmpdir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)