"""Regression tests for the three telegram_bot.py failure modes.

Each test asserts the OLD behavior is gone, not merely that the code runs.
Run: python3 test_bot_fixes.py
"""
import os
import sys
import time
import types

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "12345")
os.environ.setdefault("KYREX_TASK_TIMEOUT", "3")  # short for testing

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serve
import telegram_bot as tb

CHAT = 12345
sent = []
failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


# --- stub out all network I/O -------------------------------------------
def fake_send(chat_id, text):
    sent.append(text)
    return 999


def exploding_send(chat_id, text):
    raise RuntimeError("simulated Telegram 429")


tb.edit_message = lambda *a, **k: None


# --- Test 1: lock is released when the first sendMessage fails ----------
print("\nTest 1: busy_lock released when initial sendMessage raises")
tb.send_message = exploding_send
serve.session_lock(CHAT).acquire()  # launch() would have done this
tb.run_task(CHAT, "https://example.com/repo.git", "some task")
check("lock not stranded after send failure", not serve.session_lock(CHAT).locked(),
      "-> bot would answer 'still working' forever")


# --- Test 2: a hung child process is killed by the watchdog -------------
print(f"\nTest 2: hung agent killed by watchdog (TASK_TIMEOUT={tb.TASK_TIMEOUT}s)")
tb.send_message = fake_send
sent.clear()

hang_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_hang.py")
with open(hang_script, "w") as f:
    f.write("import time\nwhile True: time.sleep(1)\n")

real_popen = tb.subprocess.Popen


def popen_hang(cmd, **kw):
    return real_popen([sys.executable, hang_script], **kw)


tb.subprocess.Popen = popen_hang
serve.session_lock(CHAT).acquire()
t0 = time.monotonic()
tb.run_task(CHAT, "repo", "task that hangs")
elapsed = time.monotonic() - t0
tb.subprocess.Popen = real_popen

check("returned near TASK_TIMEOUT, not never",
      tb.TASK_TIMEOUT <= elapsed < tb.TASK_TIMEOUT + 10, f"(elapsed {elapsed:.1f}s)")
check("lock released after timeout", not serve.session_lock(CHAT).locked())
check("user told it was killed", any("killed" in s for s in sent), f"got {sent}")


# --- Test 3: stderr noise cannot corrupt the result JSON ---------------
print("\nTest 3: stderr interleaving does not corrupt KYREX_RESULT_JSON")
sent.clear()
noisy = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_noisy.py")
with open(noisy, "w") as f:
    f.write(
        "import sys\n"
        "for i in range(200):\n"
        "    sys.stderr.write('warning: noisy line %d\\n' % i); sys.stderr.flush()\n"
        "sys.stdout.write('KYREX_RESULT_JSON:' + '{\"status\":\"no_changes\",\"final_response\":\"all good\"}' + '\\n')\n"
        "sys.stdout.flush()\n"
    )


def popen_noisy(cmd, **kw):
    return real_popen([sys.executable, noisy], **kw)


tb.subprocess.Popen = popen_noisy
serve.session_lock(CHAT).acquire()
tb.run_task(CHAT, "repo", "task")
tb.subprocess.Popen = real_popen

check("result parsed despite stderr flood", any("all good" in s for s in sent), f"got {sent}")
check("no 'unparseable' fallback fired", not any("no parseable result" in s for s in sent))
check("lock released", not serve.session_lock(CHAT).locked())


# --- Test 4: catch_up_offset never returns 0 on failure ----------------
print("\nTest 4: catch_up_offset returns None (not 0) when Telegram unreachable")
tb.api_call = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down"))
t0 = time.monotonic()
result = tb.catch_up_offset()
check("returns None, not 0", result is None, f"got {result!r} -> 0 means REPLAY EVERYTHING")


# --- Test 5: stale pending docs expire --------------------------------
print("\nTest 5: pending documents expire after TTL")
tb.PENDING_DOC_TTL = 60
tb.pending_docs[CHAT] = [
    {"filename": "fresh.py", "content": "x", "ts": time.time()},
    {"filename": "stale.py", "content": "y", "ts": time.time() - 3600},
]
got = tb.take_pending_docs(CHAT)
check("stale doc dropped", [d["filename"] for d in got] == ["fresh.py"],
      f"got {[d['filename'] for d in got]}")
check("store cleared after take", CHAT not in tb.pending_docs)

for f in (hang_script, noisy):
    if os.path.exists(f):
        os.remove(f)

print("\n" + ("ALL TESTS PASSED" if not failures else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)
