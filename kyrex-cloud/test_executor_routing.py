"""Tests for resolve_executor and executor routing in handle_message.

Covers prefix parsing, default fallthrough, unknown-prefix rejection,
colon-in-body preservation, and single-word prefix behavior.

Run: python3 test_executor_routing.py
"""
import os
import sys

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "12345")
os.environ.setdefault("KYREX_TASK_TIMEOUT", "30")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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


def record_launch(chat_id, repo_url, task_text, executor_prefix="repo"):
    launched.append({"prefix": executor_prefix, "text": task_text, "repo": repo_url})


tb.send_message = fake_send
tb.edit_message = lambda *a, **k: None
tb.launch = record_launch
tb.busy_lock = type("Lock", (), {"locked": lambda: False, "acquire": lambda b=False: True,
                                  "release": lambda: None})()

# Ensure no aliases interfere with executor routing tests.
tb.REPO_ALIASES = {}


def reset_globals():
    sent.clear()
    launched.clear()


# --- 1. No prefix → default executor ------------------------------------
print("\nTest 1: no prefix routes to default executor with text intact")

reset_globals()
exec_prefix, rest_text, err_word = tb.resolve_executor("fix the parser")
check("resolve_executor returns default prefix",
      exec_prefix == tb.DEFAULT_EXECUTOR, f"got {exec_prefix!r}")
check("resolve_executor returns text unchanged",
      rest_text == "fix the parser", f"got {rest_text!r}")
check("resolve_executor returns no error",
      err_word is None, f"got {err_word!r}")

# Integration test: handle_message should launch with the default executor.
reset_globals()
tb.handle_message({"chat": {"id": CHAT}, "text": "fix the parser", "message_id": 1})
check("handle_message launches with default executor",
      len(launched) == 1 and launched[0]["prefix"] == tb.DEFAULT_EXECUTOR,
      f"launched={launched}")
check("handle_message passes text unchanged",
      launched and launched[0]["text"] == "fix the parser",
      f"text={launched[0]['text']!r}" if launched else "no launch")


# --- 2. Known executor prefix: repo → executor, prefix stripped --------
print("\nTest 2: 'repo: fix the parser' routes to repo executor with prefix stripped")

reset_globals()
exec_prefix, rest_text, err_word = tb.resolve_executor("repo: fix the parser")
check("resolve_executor returns the repo prefix",
      exec_prefix == "repo", f"got {exec_prefix!r}")
check("resolve_executor strips prefix from text",
      rest_text == "fix the parser", f"got {rest_text!r}")
check("resolve_executor returns no error",
      err_word is None, f"got {err_word!r}")

# handle_message integration.
reset_globals()
tb.handle_message({"chat": {"id": CHAT}, "text": "repo: fix the parser", "message_id": 2})
check("handle_message launches with 'repo' executor",
      len(launched) == 1 and launched[0]["prefix"] == "repo",
      f"launched={launched}")
check("handle_message passes stripped text",
      launched and launched[0]["text"] == "fix the parser",
      f"text={launched[0]['text']!r}" if launched else "no launch")
check("no rejection message sent",
      not any("Unknown executor" in s for s in sent),
      f"sent={sent}")


# --- 3. Unknown prefix → rejection, nothing launched ------------------
print("\nTest 3: 'mail: clear promos' sends rejection and launches nothing")

reset_globals()
exec_prefix, rest_text, err_word = tb.resolve_executor("mail: clear promos")
check("resolve_executor returns None prefix on unknown",
      exec_prefix is None, f"got {exec_prefix!r}")
check("resolve_executor returns None text on unknown",
      rest_text is None, f"got {rest_text!r}")
check("resolve_executor returns the unknown word as error",
      err_word == "mail", f"got {err_word!r}")

# handle_message integration — should send rejection but NOT launch.
reset_globals()
tb.handle_message({"chat": {"id": CHAT}, "text": "mail: clear promos", "message_id": 3})
check("a rejection message was sent",
      any("Unknown executor" in s for s in sent),
      f"sent={sent}")
check("no task was launched",
      len(launched) == 0, f"launched={launched}")


# --- 4. Colon inside text (not a prefix) → default, text intact --------
print("\nTest 4: 'fix the bug: crash on startup' routes to default with text unchanged")

reset_globals()
exec_prefix, rest_text, err_word = tb.resolve_executor("fix the bug: crash on startup")
check("resolve_executor returns default prefix (colon is not at word-boundary)",
      exec_prefix == tb.DEFAULT_EXECUTOR, f"got {exec_prefix!r}")
check("resolve_executor returns full text unchanged",
      rest_text == "fix the bug: crash on startup", f"got {rest_text!r}")
check("resolve_executor returns no error",
      err_word is None, f"got {err_word!r}")

# handle_message integration.
reset_globals()
tb.handle_message({"chat": {"id": CHAT}, "text": "fix the bug: crash on startup",
                   "message_id": 4})
check("handle_message launches with default executor",
      len(launched) == 1 and launched[0]["prefix"] == tb.DEFAULT_EXECUTOR,
      f"launched={launched}")
check("handle_message passes full text unchanged",
      launched and launched[0]["text"] == "fix the bug: crash on startup",
      f"text={launched[0]['text']!r}" if launched else "no launch")


# --- 5. Single-word prefix that is not an executor → rejected ----------
print("\nTest 5: 'fix: crash on startup' — single-word prefix, not an executor → rejected")

reset_globals()
exec_prefix, rest_text, err_word = tb.resolve_executor("fix: crash on startup")
check("resolve_executor rejects 'fix:' as unknown prefix",
      exec_prefix is None and err_word == "fix",
      f"prefix={exec_prefix!r}, err_word={err_word!r}")

# handle_message integration — currently rejected.
reset_globals()
tb.handle_message({"chat": {"id": CHAT}, "text": "fix: crash on startup",
                   "message_id": 5})
check("rejection message sent for 'fix:' prefix",
      any("Unknown executor" in s for s in sent),
      f"sent={sent}")
check("no task launched for rejected prefix",
      len(launched) == 0, f"launched={launched}")

# NOTE: This is the current behavior, but it may be a false rejection worth
# revisiting. The prefix 'fix' is a natural verb, not an executor name.
# A future enhancement could treat single-word prefixes not in EXECUTORS as
# part of the task text (falling through to the default executor) or introduce
# a configurable allowlist of known-task verbs. See also the docstring on
# resolve_executor regarding 'fix': crash on startup" vs "fix: crash on startup".


# --- Summary -----------------------------------------------------------
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)