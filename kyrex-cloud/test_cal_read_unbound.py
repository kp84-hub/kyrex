"""Regression test: an unbound session's `cal.list` read must be
operator-resolvable, not hard-denied.

Background
----------
``cal_executor.py`` emits ``KYREX_OPERATION:{"op":"cal.list",...}`` for every
``list today / tomorrow / week`` command.  In ``serve.py`` the host evaluates
the session's policy for that operation.  An *unbound* session (no Bot / no
policy — e.g. the daily ``list today`` scheduler report or a web-submitted cal
task) has an empty policy, so ``policy.evaluate`` returns ``deny``.  The
original KYREX_OPERATION handler hard-sent ``DENY`` to the executor, producing:

    calendar read denied: list today

with no way for an operator to approve it.  This mirrors the bug reported as
``calendar read denied: list today``.

The fix mirrors the KYREX_APPROVAL fallback that already existed: an unbound
``deny`` is routed to a T1 operator-resolvable approval (``APPROVE``) instead of
a hard block.  Bound sessions keep their policy-derived deny untouched.

This test exercises the full host loop (serve.run_task) with a stand-in cal
executor that speaks the real protocol, so it reproduces the reported behaviour
without any Google Calendar credentials.

Run: python3 test_cal_read_unbound.py
"""
import json
import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "12345")
os.environ.setdefault("KYREX_TASK_TIMEOUT", "30")
os.environ.setdefault("KYREX_APPROVAL_TIMEOUT", "3")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit
import serve
import telegram_bot as tb

CHAT = int(os.environ["TELEGRAM_ALLOWED_CHAT_ID"])
HERE = os.path.dirname(os.path.abspath(__file__))
failures = []
sent = []
launched = []

_next_msg_id = [3000]


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def fake_send(chat_id, text):
    _next_msg_id[0] += 1
    sent.append({"id": _next_msg_id[0], "text": text})
    return _next_msg_id[0]


tb.send_message = fake_send
tb.edit_message = lambda *a, **k: None
tb.launch = lambda chat_id, repo_url, task_text, **kw: launched.append(task_text)

real_popen = tb.subprocess.Popen


def write_executor(name, body):
    path = os.path.join(HERE, name)
    with open(path, "w") as f:
        f.write(body)
    return path


# Stand-in for cal_executor.py.  It speaks the exact protocol the real
# executor uses for a read-only calendar list:
#   KYREX_OPERATION (cal.list) -> read verdict
#     ALLOW   -> proceed, emit ok result
#     APPROVE -> emit KYREX_APPROVAL (tier 0, "calendar read") -> read 2nd line
#                proceed only on APPROVED
#     DENY / other -> refuse with "calendar read denied: list today"
CAL_EXECUTOR = (
    "import sys, json\n"
    "op = {\"op\": \"cal.list\", \"target\": \"list today\",\n"
    "      \"summary\": \"list calendar events for today\"}\n"
    "sys.stdout.write(\"KYREX_OPERATION:\" + json.dumps(op) + \"\\n\")\n"
    "sys.stdout.flush()\n"
    "verdict = sys.stdin.readline().strip()\n"
    "if verdict == \"ALLOW\":\n"
    "    sys.stdout.write(\"KYREX_RESULT_JSON:\" + json.dumps(\n"
    "        {\"status\": \"ok\", \"final_response\": \"ALLOWED\", \"errors\": []}) + \"\\n\")\n"
    "    sys.stdout.flush()\n"
    "elif verdict == \"APPROVE\":\n"
    "    approval = {\"tier\": 0, \"summary\": \"calendar read\"}\n"
    "    sys.stdout.write(\"KYREX_APPROVAL:\" + json.dumps(approval) + \"\\n\")\n"
    "    sys.stdout.flush()\n"
    "    second = sys.stdin.readline().strip()\n"
    "    if second == \"APPROVED\":\n"
    "        sys.stdout.write(\"KYREX_RESULT_JSON:\" + json.dumps(\n"
    "            {\"status\": \"ok\", \"final_response\": \"APPROVED-PROCEED\",\n"
    "             \"errors\": []}) + \"\\n\")\n"
    "        sys.stdout.flush()\n"
    "    else:\n"
    "        sys.stdout.write(\"KYREX_RESULT_JSON:\" + json.dumps(\n"
    "            {\"status\": \"error\", \"final_response\": \"\",\n"
    "             \"errors\": [\"calendar read denied: list today\"]}) + \"\\n\")\n"
    "        sys.stdout.flush()\n"
    "else:\n"
    "    sys.stdout.write(\"KYREX_RESULT_JSON:\" + json.dumps(\n"
    "        {\"status\": \"error\", \"final_response\": \"\",\n"
    "         \"errors\": [\"calendar read denied: list today\"]}) + \"\\n\")\n"
    "    sys.stdout.flush()\n"
)


def run_cal(unbound=True, policy_bot=None, replier=None, skey="cal-session"):
    """Run one cal task through serve.run_task.

    Calls ``serve.run_task`` directly so we control ``session_key`` and can
    capture the parsed ``KYREX_RESULT_JSON`` via the ``on_result`` callback
    (the host does not echo the raw JSON to chat — it formats it).

    For an *unbound* session we monkeypatch ``resolve_bot`` to return ``None``
    (no policy, rift_path None).  For a *bound* session we return the supplied
    bot dict (policy + rift_path).

    replier  -> optional threading target that delivers an operator reply.
    Returns the final result dict parsed from KYREX_RESULT_JSON.
    """
    sent.clear()
    launched.clear()
    tb.pending_approvals.clear()
    results = []
    serve.session_lock(skey).acquire()
    original_resolve = serve.resolve_bot
    if unbound:
        serve.resolve_bot = lambda sk: None
    else:
        serve.resolve_bot = lambda sk: policy_bot
    path = write_executor("_cal_standin.py", CAL_EXECUTOR)
    tb.subprocess.Popen = lambda cmd, **kw: real_popen([sys.executable, path], **kw)
    try:
        if replier is not None:
            threading.Thread(target=replier, daemon=True).start()
        serve.run_task(
            CHAT, None, "list today", executor_prefix="cal",
            send=fake_send, edit=lambda c, m, t: None,
            session_key=skey, on_result=results.append,
        )
    finally:
        serve.resolve_bot = original_resolve
        if serve.session_lock(skey).locked():
            serve.session_lock(skey).release()
        if os.path.exists(path):
            os.remove(path)
    return results[0] if results else {}


def reply_when_prompted(text, use_reply_to=True, delay=0.3):
    """Wait for a pending approval to appear, then deliver an operator reply."""
    def _run():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if tb.pending_approvals:
                break
            time.sleep(0.05)
        else:
            return
        approval_id = next(iter(tb.pending_approvals))[1]
        time.sleep(delay)
        msg = {"chat": {"id": CHAT}, "text": text, "message_id": 9999}
        if use_reply_to:
            msg["reply_to_message"] = {"message_id": approval_id}
        tb.handle_message(msg)
    return _run


def read_audit_entries():
    try:
        return audit.read_entries()
    except FileNotFoundError:
        return []


# ------------------------------------------------------------------
# Test 1: unbound session — cal.read is operator-resolvable (T1), not a
# hard deny.  This is the reported "calendar read denied: list today" bug.
# ------------------------------------------------------------------
print("\nTest 1: unbound cal.list is routed to operator approval, not denied")

with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
    tmp_path = tmp.name
audit.AUDIT_FILE = tmp_path

result = run_cal(unbound=True, replier=reply_when_prompted("y"))

check("result status is ok (not hard-denied)",
      result.get("status") == "ok", f"got {result!r}")
check("operation proceeded after operator approval",
      result.get("final_response") == "APPROVED-PROCEED",
      f"got {result.get('final_response')!r}")
check("no 'calendar read denied' error",
      not any("denied" in (e or "").lower() for e in result.get("errors", [])),
      f"errors={result.get('errors')}")

# A T1 approval prompt must have been sent to the operator.
t1_prompt = any("T1:" in m["text"] for m in sent if "T1:" in m["text"])
check("operator was prompted with a T1 approval", t1_prompt,
      f"sent messages: {[m['text'][:60] for m in sent]}")

os.unlink(tmp_path)


# ------------------------------------------------------------------
# Test 2: bound session with an explicit deny rule still hard-denies.
# Proves the fix does not weaken bound-session policy.
# ------------------------------------------------------------------
print("\nTest 2: bound session with cal:* deny rule is still hard-denied")

with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
    tmp_path = tmp.name
audit.AUDIT_FILE = tmp_path

bound_bot = {
    "id": "bound-cal-session",
    "rift": "/tmp/does-not-exist-rift",
    "policy": {"cal:*": "deny"},
}
result = run_cal(unbound=False, policy_bot=bound_bot)

check("result status is error", result.get("status") == "error",
      f"got {result!r}")
check("error mentions denied (bound policy enforced)",
      any("denied" in (e or "").lower() for e in result.get("errors", [])),
      f"errors={result.get('errors')}")
# No operator prompt should have been raised for a bound deny.
t1_prompt = any("T1:" in m["text"] for m in sent if "T1:" in m["text"])
check("no operator approval prompt for bound deny", not t1_prompt,
      f"sent messages: {[m['text'][:60] for m in sent]}")

os.unlink(tmp_path)


# ------------------------------------------------------------------
# Test 3: bound session whose policy PERMITS cal.read auto-allows (ALLOW).
# Confirms the permissive path is still intact for bound sessions.
# ------------------------------------------------------------------
print("\nTest 3: bound session with cal:* allow rule is auto-allowed")

with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
    tmp_path = tmp.name
audit.AUDIT_FILE = tmp_path

bound_bot_allow = {
    "id": "bound-cal-session-allow",
    "rift": "/tmp/does-not-exist-rift",
    "policy": {"cal:*": 0},
}
result = run_cal(unbound=False, policy_bot=bound_bot_allow)

check("result status is ok (auto-allowed by policy)",
      result.get("status") == "ok", f"got {result!r}")
check("operation proceeded (ALLOW path)",
      result.get("final_response") == "ALLOWED",
      f"got {result.get('final_response')!r}")

os.unlink(tmp_path)


# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)
