"""Run test 8 - approval wait must not consume task budget"""
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

# Test 8
print("Test 8: time spent awaiting approval must not count against TASK_TIMEOUT")
budget_probe = write_executor("_slow.py", '''
import sys, json, time
for i in range(2):
    sys.stdout.write("KYREX_APPROVAL:" + json.dumps(
        {"tier": 1, "summary": "op %d" % i, "token": ""}) + "\\n")
    sys.stdout.flush()
    sys.stdin.readline()
sys.stdout.write("KYREX_RESULT_JSON:" + json.dumps(
    {"status": "no_changes", "final_response": "survived"}) + "\\n")
sys.stdout.flush()
''')
sent.clear()
tb.pending_approvals.clear()
prev_task_timeout = tb.TASK_TIMEOUT
tb.TASK_TIMEOUT = 5          # less than 2 approval timeouts (3s each)
tb.subprocess.Popen = lambda cmd, **kw: real_popen([sys.executable, budget_probe], **kw)
tb.busy_lock.acquire()
t0 = time.monotonic()
tb.run_task(CHAT, "repo", "task")
print(f"  elapsed: {time.monotonic() - t0:.2f}s")
tb.subprocess.Popen = real_popen
tb.TASK_TIMEOUT = prev_task_timeout
# Assert on the outcome the operator cares about: the executor ran to
# completion.
survived = any("survived" in m["text"] for m in sent)
broken_pipe = any("BrokenPipe" in m["text"] for m in sent)
check("executor ran to completion despite two approval waits",
      survived, "-> watchdog counts human think-time against the task budget")
check("no BrokenPipe from writing to a watchdog-killed executor",
      not broken_pipe)
check("lock released", not tb.busy_lock.locked())

for f in ("_slow.py",):
    p = os.path.join(HERE, f)
    if os.path.exists(p):
        os.remove(p)

print("\n" + ("TEST 8 PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)