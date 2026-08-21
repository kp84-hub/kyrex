"""Cross-session isolation for serve.py.

The four existing suites all run in one session, so none of them can catch a
leak between sessions. These assertions are the reason per-session locking and
(session_key, message_id) approval keys exist at all: without them a bare "y"
in one chat could resolve a destructive approval pending in another.

Run: python3 test_session_isolation.py
"""
import os
import sys
import threading

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "12345")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serve

A, B = "chatA", "chatB"
failures = []


def check(name, cond, detail=""):
    if cond:
        print("  PASS  " + name)
    else:
        print("  FAIL  " + name + (" " + detail if detail else ""))
        failures.append(name)


print("\nTest 1: each session gets its own lock")
la, lb = serve.session_lock(A), serve.session_lock(B)
check("distinct sessions get distinct locks", la is not lb)
check("same key returns the same lock", serve.session_lock(A) is la)
check("int and str keys agree", serve.session_lock(12345) is serve.session_lock("12345"))

print("\nTest 2: one busy session does not block another")
la.acquire()
check("session A reports busy", serve.session_busy(A))
check("session B still free", not serve.session_busy(B))
check("any_session_busy sees it", serve.any_session_busy())
la.release()
check("A free after release", not serve.session_busy(A))

print("\nTest 3: an approval in A cannot be resolved from B")
serve.pending_approvals.clear()
evt_a = threading.Event()
serve.pending_approvals[(A, 900)] = {"event": evt_a, "chat_id": A, "tier": 1,
                                     "token": "", "result": None}
consumed = serve.handle_approval_reply(B, "y", None, session_key=B)
check("bare 'y' in B is not consumed", not consumed)
check("A's approval untouched", not evt_a.is_set(),
      "-> cross-session approval leak")

print("\nTest 4: identical message ids in two sessions stay distinct")
evt_b = threading.Event()
serve.pending_approvals[(B, 900)] = {"event": evt_b, "chat_id": B, "tier": 1,
                                     "token": "", "result": None}
serve.handle_approval_reply(B, "y", 900, session_key=B)
check("reply_to 900 in B resolves B's approval", evt_b.is_set())
check("A's identically-numbered approval untouched", not evt_a.is_set(),
      "-> message_id collision across sessions")

print("\nTest 5: session scoping is not so strict it breaks bare replies")
serve.pending_approvals.clear()
evt_a2 = threading.Event()
serve.pending_approvals[(A, 1)] = {"event": evt_a2, "chat_id": A, "tier": 1,
                                   "token": "", "result": None}
serve.pending_approvals[(B, 2)] = {"event": threading.Event(), "chat_id": B,
                                   "tier": 1, "token": "", "result": None}
serve.handle_approval_reply(A, "y", None, session_key=A)
check("bare 'y' resolves A's sole approval despite B also pending",
      evt_a2.is_set(), "-> session scoping too strict")
serve.pending_approvals.clear()

print("\n" + ("ALL TESTS PASSED" if not failures
              else "%d FAILURE(S): %s" % (len(failures), failures)))
sys.exit(1 if failures else 0)
