"""Tests for slash-command handling in handle_message.

Covers: /status, /repos, unknown slash commands (rejection + no launch),
and normal messages (no leading slash) still launching tasks.

Run: python3 test_slash_commands.py
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


def record_launch(chat_id, repo_url, task_text, executor_prefix="repo", session_key=None):
    launched.append({"prefix": executor_prefix, "text": task_text, "repo": repo_url})


tb.send_message = fake_send
tb.edit_message = lambda *a, **k: None
tb.launch = record_launch
tb.busy_lock = type("Lock", (), {"locked": lambda: False, "acquire": lambda b=False: True,
                                  "release": lambda: None})()

# Ensure no aliases interfere.
tb.REPO_ALIASES = {}


def reset_globals():
    sent.clear()
    launched.clear()


# --- 1. /status replies and does NOT launch a task ------------------------
print("\nTest 1: /status replies with busy/idle status and launches nothing")

reset_globals()
tb.handle_message({"chat": {"id": CHAT}, "text": "/status", "message_id": 1})
check("/status sent a reply",
      len(sent) >= 1,
      f"sent={sent}")
check("/status reply mentions idle or busy",
      sent and ("idle" in sent[0] or "busy" in sent[0]),
      f"sent={sent!r}")
check("/status did not launch a task",
      len(launched) == 0,
      f"launched={launched}")


# --- 2. /repos replies and does NOT launch a task -------------------------
print("\nTest 2: /repos replies with repo list and launches nothing")

reset_globals()
tb.handle_message({"chat": {"id": CHAT}, "text": "/repos", "message_id": 2})
check("/repos sent a reply",
      len(sent) >= 1,
      f"sent={sent}")
check("/repos reply mentions 'Default'",
      sent and "Default" in sent[0],
      f"sent={sent!r}")
check("/repos did not launch a task",
      len(launched) == 0,
      f"launched={launched}")


# --- 3. Unknown slash command → command list, no launch ------------------
print("\nTest 3: /start replies with valid command list and launches nothing")

reset_globals()
tb.handle_message({"chat": {"id": CHAT}, "text": "/start", "message_id": 3})
check("/start sent a reply",
      len(sent) >= 1,
      f"sent={sent}")
check("/start reply mentions valid commands",
      sent and "Valid commands" in sent[0],
      f"sent={sent!r}")
check("/start reply mentions /status",
      sent and "/status" in sent[0],
      f"sent={sent!r}")
check("/start reply mentions /repos",
      sent and "/repos" in sent[0],
      f"sent={sent!r}")
check("/start did not launch a task",
      len(launched) == 0,
      f"launched={launched}")


# --- 4. Another unknown slash command → same behavior --------------------
print("\nTest 4: /unknown also replies with command list and launches nothing")

reset_globals()
tb.handle_message({"chat": {"id": CHAT}, "text": "/unknown", "message_id": 4})
check("/unknown sent a reply",
      len(sent) >= 1,
      f"sent={sent}")
check("/unknown reply mentions valid commands",
      sent and "Valid commands" in sent[0],
      f"sent={sent!r}")
check("/unknown did not launch a task",
      len(launched) == 0,
      f"launched={launched}")


# --- 5. Normal message (no leading slash) still launches task ------------
print("\nTest 5: 'fix the parser' (no leading slash) launches a task")

reset_globals()
tb.handle_message({"chat": {"id": CHAT}, "text": "fix the parser", "message_id": 5})
check("normal message launches a task",
      len(launched) == 1,
      f"launched={launched}")
check("launched task has default executor prefix",
      launched and launched[0]["prefix"] == tb.DEFAULT_EXECUTOR,
      f"prefix={launched[0]['prefix']!r}" if launched else "no launch")
check("launched text is the original message",
      launched and launched[0]["text"] == "fix the parser",
      f"text={launched[0]['text']!r}" if launched else "no launch")
check("no rejection message sent for normal text",
      not any("Unknown" in s for s in sent),
      f"sent={sent}")


# --- Summary -------------------------------------------------------------
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)