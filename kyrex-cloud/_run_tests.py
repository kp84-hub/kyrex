"""Run tests one by one to see which pass"""
import os, sys, threading, time
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "12345")
os.environ.setdefault("KYREX_TASK_TIMEOUT", "30")
os.environ.setdefault("KYREX_APPROVAL_TIMEOUT", "3")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import telegram_bot as tb
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
tb.launch = lambda chat_id, repo_url, task_text: launched.append(task_text)
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
    sent.clear()
    launched.clear()
    tb.pending_approvals.clear()
    path = write_executor("_approver.py", APPROVING_EXECUTOR % {"tier": tier, "token": token})
    tb.subprocess.Popen = lambda cmd, **kw: real_popen([sys.executable, path], **kw)
    tb.busy_lock.acquire()
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
    def _run():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if tb.pending_approvals:
                break
            time.sleep(0.05)
        else:
            return
        approval_id = next(iter(tb.pending_approvals))
        time.sleep(delay)
        msg = {"chat": {"id": CHAT}, "text": text, "message_id": 9999}
        if use_reply_to:
            msg["reply_to_message"] = {"message_id": approval_id}
        tb.handle_message(msg)
    return _run

# Test 1
print("\nTest 1: tier 1 — 'y' as a reply-to approves")
elapsed, decision = run_with_executor(1, "", reply_when_prompted("y"))
check("executor received APPROVED", decision == "APPROVED", f"got {decision!r}")
check("lock released", not tb.busy_lock.locked())
check("returned before timeout", elapsed < tb.APPROVAL_TIMEOUT, f"({elapsed:.1f}s)")

# Test 2
print("\nTest 2: tier 1 — 'n' denies")
elapsed, decision = run_with_executor(1, "", reply_when_prompted("n"))
check("executor received DENIED", decision == "DENIED", f"got {decision!r}")
check("lock released", not tb.busy_lock.locked())

# Test 3
print("\nTest 3: tier 2 — exact token approves, near-miss denies")
elapsed, decision = run_with_executor(2, "TRASH 1247", reply_when_prompted("TRASH 1247"))
check("exact token approves", decision == "APPROVED", f"got {decision!r}")
check("lock released", not tb.busy_lock.locked())
elapsed, decision = run_with_executor(2, "TRASH 1247", reply_when_prompted("y"))
check("'y' does NOT approve a T2 op", decision == "DENIED", f"got {decision!r}")
check("lock released", not tb.busy_lock.locked())
elapsed, decision = run_with_executor(2, "TRASH 1247", reply_when_prompted("trash 1247"))
check("wrong case does NOT approve", decision == "DENIED", f"got {decision!r}")

# Test 4
print(f"\nTest 4: no reply — host denies after APPROVAL_TIMEOUT={tb.APPROVAL_TIMEOUT}s")
elapsed, decision = run_with_executor(1, "", None)
check("executor received DENIED", decision == "DENIED", f"got {decision!r}")
check("waited ~APPROVAL_TIMEOUT, not forever",
      tb.APPROVAL_TIMEOUT <= elapsed < tb.APPROVAL_TIMEOUT + 10, f"({elapsed:.1f}s)")
check("lock released after timeout", not tb.busy_lock.locked())
check("pending_approvals cleaned up", not tb.pending_approvals, f"{tb.pending_approvals}")

# Test 5
print("\nTest 5: executor exits while approval pending — lock still released")
sent.clear()
tb.pending_approvals.clear()
dying = write_executor("_dying.py", '''
import sys, json
sys.stdout.write("KYREX_APPROVAL:" + json.dumps(
    {"tier": 1, "summary": "op", "token": ""}) + "\\n")
sys.stdout.flush()
sys.exit(1)
''')
tb.subprocess.Popen = lambda cmd, **kw: real_popen([sys.executable, dying], **kw)
tb.busy_lock.acquire()
done = threading.Event()
threading.Thread(target=lambda: (tb.run_task(CHAT, "repo", "t"), done.set()), daemon=True).start()
finished = done.wait(timeout=tb.APPROVAL_TIMEOUT + 15)
tb.subprocess.Popen = real_popen
check("run_task returned (did not hang)", finished)
check("lock released", not tb.busy_lock.locked())

# Test 6
print("\nTest 6: bare 'y' (no reply-to) must not be launched as a git task")
elapsed, decision = run_with_executor(1, "", reply_when_prompted("y", use_reply_to=False))
check("no task launched from the approval reply", not launched,
      f"launched {launched!r} -> bot would start a git task named 'y'")

# Test 7
print("\nTest 7: reply arriving after timeout must not be launched as a task")
sent.clear()
launched.clear()
tb.pending_approvals.clear()
elapsed, decision = run_with_executor(1, "", None)  # times out, clears pending
tb.handle_message({"chat": {"id": CHAT}, "text": "y", "message_id": 8888,
                   "reply_to_message": {"message_id": 1001}})
check("stale approval reply not launched as a task", not launched,
      f"launched {launched!r}")

# Cleanup
for f in ("_approver.py", "_dying.py"):
    p = os.path.join(HERE, f)
    if os.path.exists(p):
        os.remove(p)

print("\n" + ("TESTS 1-7 PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)