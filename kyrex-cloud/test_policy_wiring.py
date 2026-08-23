"""Tests: policy evaluation wiring in serve.py for every approval.

Covers: dry_run mode returns the derived tier unchanged regardless of what
policy says, the audit entry carries the policy reason, and a raising
policy.evaluate does not block the approval.

Run: python3 test_policy_wiring.py
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
import policy
import serve
import telegram_bot as tb

CHAT = int(os.environ["TELEGRAM_ALLOWED_CHAT_ID"])
HERE = os.path.dirname(os.path.abspath(__file__))
failures = []
sent = []
launched = []

_next_msg_id = [2000]


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
req = {"tier": %(tier)s, "summary": "%(summary)s", "token": "%(token)s"}
sys.stdout.write("KYREX_APPROVAL:" + json.dumps(req) + "\\n")
sys.stdout.flush()
decision = sys.stdin.readline().strip()
sys.stdout.write("KYREX_RESULT_JSON:" + json.dumps(
    {"status": "no_changes", "final_response": "decision=" + decision}) + "\\n")
sys.stdout.flush()
'''


def run_with_executor(executor_prefix, tier, summary="test op", token="",
                      replier=None, prefix="repo"):
    """Run one task against an approving executor. Returns (elapsed, decision_seen)."""
    sent.clear()
    launched.clear()
    tb.pending_approvals.clear()
    path = write_executor(
        "_policy_approver.py",
        APPROVING_EXECUTOR % {"tier": tier, "summary": summary, "token": token},
    )
    tb.subprocess.Popen = lambda cmd, **kw: real_popen([sys.executable, path], **kw)
    serve.session_lock(CHAT).acquire()
    if replier:
        threading.Thread(target=replier, daemon=True).start()
    t0 = time.monotonic()
    tb.run_task(CHAT, "repo", "task", executor_prefix=prefix)
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
    try:
        return audit.read_entries()
    except FileNotFoundError:
        return []


# ------------------------------------------------------------------
# Test 1: dry_run — tier used is the derived tier regardless of policy
# ------------------------------------------------------------------
print("\nTest 1: dry_run mode returns derived tier even when policy would change it")

# Save original mode and set dry_run
original_mode = policy.MODE
policy.MODE = "dry_run"

with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
    tmp_path = tmp.name
audit.AUDIT_FILE = tmp_path

# Use an executor with tier=1 and a destructive summary "delete".
# derive_tier will bump it to 2 (destructive verb).
# Policy (empty dict) would deny (effective_tier="deny"),
# but in dry_run enforce returns the derived_tier unchanged (2).
elapsed, decision = run_with_executor(
    executor_prefix="fs", tier=1, summary="delete important file", token="X",
    replier=reply_when_prompted("X"),
    prefix="fs",
)

check("executor received APPROVED", decision == "APPROVED", f"got {decision!r}")

# The approval prompt should show T2 because the tier used is 2 (derived from
# destructive verb), not "deny" which would be the policy's effective_tier.
tier_in_prompt = any("T2:" in m["text"] for m in sent if "T2:" in m["text"])
check("approval prompt shows T2 (derived tier) not deny", tier_in_prompt,
      f"sent messages: {[m['text'][:60] for m in sent]}")

# The audit entry should carry policy info.
entries = read_audit_entries()
check("at least one audit entry written", len(entries) >= 1, f"got {len(entries)}")
if entries:
    e = entries[0]
    detail = e.get("detail")
    check("detail field present with policy info", detail is not None,
          f"got detail={detail!r}")
    if detail:
        policy_field = detail if isinstance(detail, dict) else json.loads(detail)
        matched_rule = policy_field.get("policy", {}).get("matched_rule")
        reason = policy_field.get("policy", {}).get("reason")
        check("matched_rule is None (empty policy)", matched_rule is None,
              f"got {matched_rule!r}")
        check("reason mentions 'no matching rule'",
              reason and "no matching rule" in reason,
              f"got {reason!r}")

os.unlink(tmp_path)

# ------------------------------------------------------------------
# Test 2: audit entry carries the policy reason
# ------------------------------------------------------------------
print("\nTest 2: audit entry carries the policy reason in non-dry-run mode")

policy.MODE = "enforce"

with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
    tmp_path = tmp.name
audit.AUDIT_FILE = tmp_path

elapsed, decision = run_with_executor(
    executor_prefix="fs", tier=1, summary="read config", token="Y",
    replier=reply_when_prompted("Y"),
    prefix="fs",
)

entries = read_audit_entries()
check("audit entry written", len(entries) >= 1, f"got {len(entries)}")
if entries:
    e = entries[0]
    detail = e.get("detail")
    check("detail field present", detail is not None, f"got {detail!r}")
    if detail:
        policy_field = detail if isinstance(detail, dict) else json.loads(detail)
        reason = policy_field.get("policy", {}).get("reason")
        check("reason is a non-empty string",
              reason and len(reason) > 0, f"got {reason!r}")

os.unlink(tmp_path)

# ------------------------------------------------------------------
# Test 3: raising policy.evaluate still lets approval proceed
# ------------------------------------------------------------------
print("\nTest 3: raising policy.evaluate does not block the approval")

policy.MODE = "dry_run"

# Monkey-patch policy.evaluate to raise
original_evaluate = policy.evaluate
call_count = [0]

def raising_evaluate(policy, operation, derived_tier):
    call_count[0] += 1
    raise RuntimeError("simulated evaluate crash")

policy.evaluate = raising_evaluate

with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
    tmp_path = tmp.name
audit.AUDIT_FILE = tmp_path

elapsed, decision = run_with_executor(
    executor_prefix="fs", tier=1, summary="delete config", token="Z",
    replier=reply_when_prompted("Z"),
    prefix="fs",
)

check("policy.evaluate was called", call_count[0] >= 1, f"called {call_count[0]} times")
check("executor received APPROVED despite policy crash",
      decision == "APPROVED", f"got {decision!r}")

# The approval prompt should still use the derived tier (2 for destructive verb)
tier2_prompt = any("T2:" in m["text"] for m in sent if "T2:" in m["text"])
check("approval shows derived tier despite policy crash", tier2_prompt,
      f"sent: {[m['text'][:60] for m in sent]}")

# Restore
policy.evaluate = original_evaluate
os.unlink(tmp_path)

# ------------------------------------------------------------------
# Cleanup
# ------------------------------------------------------------------
policy.MODE = original_mode

for f in ("_policy_approver.py",):
    p = os.path.join(HERE, f)
    if os.path.exists(p):
        os.remove(p)

print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)