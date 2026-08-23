"""Regression tests: audit wiring in serve.py records every approval decision.

Tests that audit.log is called with the correct decision for each of the three
approval outcomes (approved, denied, timeout) and that an audit log failure
does not prevent the decision from reaching the executor.

Run: python3 test_audit_wiring.py
"""
import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "12345")
os.environ.setdefault("KYREX_TASK_TIMEOUT", "30")
os.environ.setdefault("KYREX_APPROVAL_TIMEOUT", "3")  # short for testing

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit
import serve
import telegram_bot as tb

# These tests exercise approval routing - does a reply resolve the right
# pending approval, does a token match - not authorization. Policy must not
# decide their outcome, so pin dry_run: in enforce mode an unbound session
# with an empty policy denies by default and every approval never happens.
import policy as _policy
_policy.MODE = "dry_run"

CHAT = int(os.environ["TELEGRAM_ALLOWED_CHAT_ID"])
HERE = os.path.dirname(os.path.abspath(__file__))
failures = []
sent = []
launched = []

_next_msg_id = [1000]


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


APPROVING_EXECUTOR = '''
import sys, json
req = {"tier": %(tier)s, "summary": "test op", "token": "%(token)s"}
sys.stdout.write("KYREX_APPROVAL:" + json.dumps(req) + "\\n")
sys.stdout.flush()
decision = sys.stdin.readline().strip()
sys.stdout.write("KYREX_RESULT_JSON:" + json.dumps(
    {"status": "no_changes", "final_response": "decision=" + decision}) + "\\n")
sys.stdout.flush()
'''


def run_with_executor(tier, token, replier=None):
    """Run one task against an approving executor. Returns (elapsed, decision_seen)."""
    sent.clear()
    launched.clear()
    tb.pending_approvals.clear()
    path = write_executor("_audit_approver.py", APPROVING_EXECUTOR % {"tier": tier, "token": token})
    tb.subprocess.Popen = lambda cmd, **kw: real_popen([sys.executable, path], **kw)
    serve.session_lock(CHAT).acquire()
    if replier:
        threading.Thread(target=replier, daemon=True).start()
    t0 = time.monotonic()
    tb.run_task(CHAT, "repo", "task")
    elapsed = time.monotonic() - t0
    tb.subprocess.Popen = real_popen
    decision = None
    for m in sent:
        if "decision=" in m["text"]:
            decision = m["text"].split("decision=")[1].split()[0].strip()
    return elapsed, decision


def reply_when_prompted(text, use_reply_to=True, delay=0.4):
    """Wait for an approval to be registered, then deliver an operator reply."""
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
    """Read entries from the current audit file, newest first."""
    try:
        return audit.read_entries()
    except FileNotFoundError:
        return []


# --- Test 1: approved writes an audit entry -------------------------------
print("\nTest 1: approved operation writes a 'approved' audit entry")
with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
    tmp_path = tmp.name
audit.AUDIT_FILE = tmp_path
elapsed, decision = run_with_executor(1, "", reply_when_prompted("y"))
entries = read_audit_entries()
check("executor received APPROVED", decision == "APPROVED", f"got {decision!r}")
check("one audit entry written", len(entries) == 1, f"got {len(entries)}")
if entries:
    e = entries[0]
    check("decision field is 'approved'", e.get("decision") == "approved", f"got {e.get('decision')!r}")
    check("bot_id equals executor prefix", e.get("bot_id") == "repo", f"got {e.get('bot_id')!r}")
    check("operation equals approval summary", e.get("operation") == "test op", f"got {e.get('operation')!r}")
    check("tier is set", e.get("tier") == "tier1", f"got {e.get('tier')!r}")
    check("outcome field present", e.get("outcome") == "pending", f"got {e.get('outcome')!r}")
os.unlink(tmp_path)


# --- Test 2: denied writes an audit entry ---------------------------------
print("\nTest 2: denied operation writes a 'denied' audit entry")
with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
    tmp_path = tmp.name
audit.AUDIT_FILE = tmp_path
elapsed, decision = run_with_executor(1, "", reply_when_prompted("n"))
entries = read_audit_entries()
check("executor received DENIED", decision == "DENIED", f"got {decision!r}")
check("one audit entry written", len(entries) == 1, f"got {len(entries)}")
if entries:
    e = entries[0]
    check("decision field is 'denied'", e.get("decision") == "denied", f"got {e.get('decision')!r}")
    check("bot_id equals executor prefix", e.get("bot_id") == "repo", f"got {e.get('bot_id')!r}")
    check("operation equals approval summary", e.get("operation") == "test op", f"got {e.get('operation')!r}")
os.unlink(tmp_path)


# --- Test 3: timeout writes an audit entry ---------------------------------
print(f"\nTest 3: timeout writes a 'timeout' audit entry (timeout={tb.APPROVAL_TIMEOUT}s)")
with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
    tmp_path = tmp.name
audit.AUDIT_FILE = tmp_path
elapsed, decision = run_with_executor(1, "", None)  # no replier -> times out
entries = read_audit_entries()
check("executor received DENIED on timeout", decision == "DENIED", f"got {decision!r}")
check("one audit entry written", len(entries) == 1, f"got {len(entries)}")
if entries:
    e = entries[0]
    check("decision field is 'timeout'", e.get("decision") == "timeout", f"got {e.get('decision')!r}")
    check("bot_id equals executor prefix", e.get("bot_id") == "repo", f"got {e.get('bot_id')!r}")
    check("operation equals approval summary", e.get("operation") == "test op", f"got {e.get('operation')!r}")
os.unlink(tmp_path)


# --- Test 4: audit failure does not block the decision --------------------
print("\nTest 4: audit log failure must not block the decision from reaching the executor")
original_log = audit.log
log_called = threading.Event()
call_args = []

def failing_log(bot_id, operation, tier, decision, outcome, detail=None,
                **kwargs):
    # **kwargs so a new audit field does not turn this into a TypeError,
    # which the caller's except would swallow as an audit failure and the
    # test would pass without exercising the path it exists to check.
    call_args.append((bot_id, operation, tier, decision, outcome))
    log_called.set()
    raise RuntimeError("simulated audit failure")

audit.log = failing_log
with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
    tmp_path = tmp.name
audit.AUDIT_FILE = tmp_path
elapsed, decision = run_with_executor(1, "", reply_when_prompted("y"))
check("executor received APPROVED despite audit failure",
      decision == "APPROVED", f"got {decision!r}")
check("audit.log was actually called",
      log_called.is_set(), "audit.log was never called")
check("lock released", not serve.session_lock(CHAT).locked())
audit.log = original_log
os.unlink(tmp_path)


# Cleanup temporary executor script
for f in ("_audit_approver.py",):
    p = os.path.join(HERE, f)
    if os.path.exists(p):
        os.remove(p)

print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)