#!/usr/bin/env python3
"""Web user ↔ Bot-ID collision / isolation — reproduction + regression.

Reported finding (Medium): a web task uses the authenticated GitHub username
as its ``session_key``; ``_resolve_bot``/``build_context`` resolve a Bot whose
id equals that username; therefore a Bot whose ID equals a GitHub username may
accidentally bind a web task to that Bot's Rift, repository, policy, or other
Bot-specific state.

Production call path exercised end-to-end (the real one):

    web/backend/main.py  POST /api/task
      -> CloudTaskStore.submit(session_key=<github username>, ...)
        -> _resolve_bot(session_key)  [task row identity chain]
      -> TaskWorker.execute_task(task)
        -> serve.run_task(session_key=<username>)
          -> build_context(session_key)  [executor sees KYREX_FS_ROOT / --rift]
          -> git_workflow subprocess (here a recording stand-in)

Tests
-----
A. Web user "octo", no Bot registered -> normal unbound web behaviour.
B. Bot "octo" registered, then a web task for user "octo" -> the web task
   MUST remain unbound (no Bot Rift/policy/identity inheritance).  This is
   the regression assertion for the collision fix: web submissions pass
   resolve_bot=False (task row stays unbound) and the worker runs them with
   bot resolution disabled, so the executor never sees the Bot's context.
C. Compare B against the intended unbound web context (must be identical).
D. The two contracts are verified separately after the collision fix:
   D1. A web task for user "octo" stays unbound even while Bot "octo" is
       registered.  The web helper carries no resolve_bot knob at all —
       resolve_bot=False is the sealed web contract, so a future edit
       cannot "prove" bot binding through the web submission path.
   D2. A legitimate Bot session (session_key = the Bot's own identity,
       bot identity chain recorded at submit) still resolves to Bot "octo"
       at execution and receives its Rift/policy, and passes --rift /
       KYREX_FS_ROOT to the executor.
E. The production web endpoint (web/backend/main.py POST /api/task) must
   keep submitting with resolve_bot=False — a source-level guard so a future
   edit to the endpoint cannot silently re-open the collision.

Run: python3 test_web_bot_collision.py
"""
import importlib
import json
import os
import sys
import tempfile
import uuid

# Isolate data (bots registry + task DB) before importing the modules.
_TMP = tempfile.mkdtemp(prefix="kyrex_web_bot_col_")
os.environ["KYREX_DATA_DIR"] = _TMP
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bots            # noqa: E402
import serve           # noqa: E402
import task_store as ts  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


# ── Recording stand-in for the repo executor subprocess ─────────────────
# serve.run_task spawns EXECUTORS["repo"] as a subprocess with --rift and
# KYREX_FS_ROOT when a Bot is bound.  This stand-in writes exactly what the
# real git_workflow.py would observe (argv + env) to a JSON report file, so
# the test can prove which context the production executor receives.
REPORT = os.path.join(_TMP, "executor_report.json")
FAKE_REPO = os.path.join(_TMP, "fake_repo_executor.py")
with open(FAKE_REPO, "w") as f:
    f.write(
        "import json, os, sys\n"
        "report = os.environ.get('KYREX_EXECUTOR_REPORT')\n"
        "if report:\n"
        "    with open(report, 'w') as fh:\n"
        "        json.dump({'argv': sys.argv[1:],\n"
        "                   'fs_root': os.environ.get('KYREX_FS_ROOT')},\n"
        "                  fh)\n"
        "sys.stdout.write('KYREX_RESULT_JSON:{\"status\": \"done\"}\\n')\n"
        "sys.stdout.flush()\n"
    )
serve.EXECUTORS["repo"] = FAKE_REPO


def reset_registry():
    for b in list(bots.load_bots()):
        bots.remove_bot(b)


def _submit_and_run(username, task_text="web task"):
    """Exactly what the web backend does, minus HTTP: submit with the
    authenticated GitHub username as session_key and resolve_bot=False.
    resolve_bot=False is the sealed web contract — web sessions never
    resolve Bots by username, and this helper deliberately exposes no
    knob to re-enable it.  Then run via the real TaskWorker ->
    serve.run_task path.  Returns (task, ctx_capture, report)."""
    store = ts.CloudTaskStore()
    os.environ["KYREX_EXECUTOR_REPORT"] = REPORT
    if os.path.exists(REPORT):
        os.remove(REPORT)
    tid = store.submit(
        session_key=username,
        task_text=task_text,
        repo_url="https://github.com/example/web-repo.git",
        executor_prefix="repo",
        chat_id=username,
        resolve_bot=False,  # web contract: a GitHub username never binds a Bot
    )
    # Instrument ctx building exactly as serve.run_task does it, without
    # changing production code: wrap build_context and record what it returns.
    captured = {}
    original = serve.build_context

    def _wrapped(skey, prefix="repo", **extra):
        ctx = original(skey, prefix, **extra)
        captured["ctx"] = {
            "bot_id": ctx.bot_id,
            "rift_path": ctx.rift_path,
            "policy": dict(ctx.policy),
        }
        return ctx

    serve.build_context = _wrapped
    try:
        worker = ts.TaskWorker(store, worker_id="w-" + uuid.uuid4().hex[:6])
        worker.claim_and_execute_once(timeout=8.0)
    finally:
        serve.build_context = original
    task = store.get(tid)
    report = None
    if os.path.exists(REPORT):
        with open(REPORT) as fh:
            report = json.load(fh)
    store.close()
    return task, captured, report


def _submit_and_run_bot(bot_id, task_text="bot task"):
    """Legitimate Bot-bound submission (the Telegram/bot path): session_key
    IS the Bot's own identity and the dispatcher records the Bot identity
    chain (bot_id/bot_prefix/rift) at submit time.  Resolution stays enabled
    at execution, so serve still resolves the Bot from the registry by its
    identity and delivers its Rift/policy to the executor.
    Returns (task, ctx_capture, report)."""
    entry = bots.load_bots().get(bot_id) or {}
    store = ts.CloudTaskStore()
    os.environ["KYREX_EXECUTOR_REPORT"] = REPORT
    if os.path.exists(REPORT):
        os.remove(REPORT)
    tid = store.submit(
        session_key=bot_id,
        task_text=task_text,
        repo_url="https://github.com/example/web-repo.git",
        executor_prefix="repo",
        bot_id=bot_id,
        bot_prefix=f"@{bot_id}",
        rift=entry.get("rift"),
        chat_id=bot_id,
    )
    captured = {}
    original = serve.build_context

    def _wrapped(skey, prefix="repo", **extra):
        ctx = original(skey, prefix, **extra)
        captured["ctx"] = {
            "bot_id": ctx.bot_id,
            "rift_path": ctx.rift_path,
            "policy": dict(ctx.policy),
        }
        return ctx

    serve.build_context = _wrapped
    try:
        worker = ts.TaskWorker(store, worker_id="w-" + uuid.uuid4().hex[:6])
        worker.claim_and_execute_once(timeout=8.0)
    finally:
        serve.build_context = original
    task = store.get(tid)
    report = None
    if os.path.exists(REPORT):
        with open(REPORT) as fh:
            report = json.load(fh)
    store.close()
    return task, captured, report


print("\n=== Area 1: web user ↔ bot-id collision ===")

# ── Test A: web user "octo", no bot ───────────────────────────────────────
print("\nA. web user 'octo' with NO bot registered (intended unbound web)")
reset_registry()
task_a, cap_a, rep_a = _submit_and_run("octo")
check("A: task row records no bot_id", task_a["bot_id"] is None, f"got {task_a['bot_id']}")
check("A: task row records no rift", task_a["rift"] is None, f"got {task_a['rift']}")
check("A: exec ctx is unbound (bot_id=executor_prefix)", cap_a["ctx"]["bot_id"] == "repo",
      f"got {cap_a['ctx']['bot_id']}")
check("A: ctx has no rift_path", cap_a["ctx"]["rift_path"] is None,
      f"got {cap_a['ctx']['rift_path']}")
check("A: ctx carries UNBOUND_POLICY", cap_a["ctx"]["policy"] == dict(serve.UNBOUND_POLICY),
      f"got {cap_a['ctx']['policy']}")
check("A: executor receives NO --rift", rep_a and "--rift" not in rep_a["argv"],
      f"argv={rep_a and rep_a['argv']}")
check("A: executor receives NO KYREX_FS_ROOT", rep_a and rep_a["fs_root"] is None,
      f"fs_root={rep_a and rep_a['fs_root']}")

# ── Test B: bot "octo" registered, web user "octo" (the collision) ───────
print("\nB. web user 'octo' WITH bot 'octo' registered (collision must NOT bind)")
bots.add_bot("octo", "Octo Bot", "m",
              "/tmp/rift-octo", policy={"fs:read": 0, "fs:write": 1, "repo:pr": 1})
task_b, cap_b, rep_b = _submit_and_run("octo")
check("B: task row NOT bound to any bot", task_b["bot_id"] is None,
      f"got {task_b['bot_id']}")
check("B: task row carries NO bot rift", task_b["rift"] is None,
      f"got {task_b['rift']}")
check("B: ctx.bot_id is the UNBOUND executor identity, not the bot",
      cap_b["ctx"]["bot_id"] == "repo",
      f"got {cap_b['ctx']['bot_id']}")
check("B: ctx.rift_path is None (no bot rift inherited)",
      cap_b["ctx"]["rift_path"] is None,
      f"got {cap_b['ctx']['rift_path']}")
check("B: ctx.policy is UNBOUND_POLICY, not the bot policy",
      cap_b["ctx"]["policy"] == dict(serve.UNBOUND_POLICY),
      f"got {cap_b['ctx']['policy']}")
check("B: executor receives NO --rift", rep_b and "--rift" not in rep_b["argv"],
      f"argv={rep_b and rep_b['argv']}")
check("B: executor receives NO KYREX_FS_ROOT",
      rep_b and rep_b["fs_root"] is None,
      f"fs_root={rep_b and rep_b['fs_root']}")

# ── Test C: collided web task must be identical to the unbound web task ────
print("\nC. comparison: collided web ctx vs no-bot web ctx (must be IDENTICAL)")
check("C: rift identical (unbound in both cases)",
      cap_a["ctx"]["rift_path"] == cap_b["ctx"]["rift_path"] is None,
      f"a={cap_a['ctx']['rift_path']} b={cap_b['ctx']['rift_path']}")
check("C: policy identical (UNBOUND in both cases)",
      cap_a["ctx"]["policy"] == cap_b["ctx"]["policy"] == dict(serve.UNBOUND_POLICY),
      f"a={cap_a['ctx']['policy']} b={cap_b['ctx']['policy']}")
check("C: identity identical (both executed unbound)",
      cap_a["ctx"]["bot_id"] == cap_b["ctx"]["bot_id"] == "repo",
      f"a={cap_a['ctx']['bot_id']} b={cap_b['ctx']['bot_id']}")

# ── Test D: the two contracts stay separate after the collision fix ────────
# Bot "octo" registered in Test B is still registered here (no registry
# reset between B and D), so D1 and D2 run against the same collision state.
# D1: the web submission path is sealed — its helper exposes no resolve_bot
#     knob, so a web task for "octo" can never bind even while Bot "octo"
#     exists.
# D2: a legitimate Bot session (session_key = bot identity "octo",
#     identity chain recorded at submit) still resolves to Bot "octo" at
#     execution and receives its Rift/policy + --rift / KYREX_FS_ROOT.
print("\nD1. web 'octo' stays unbound while bot 'octo' exists (sealed web path)")
task_d1, cap_d1, rep_d1 = _submit_and_run("octo")
check("D1: web task row NOT bound to bot 'octo'", task_d1["bot_id"] is None,
      f"got {task_d1['bot_id']}")
check("D1: web task row carries NO bot rift", task_d1["rift"] is None,
      f"got {task_d1['rift']}")
check("D1: ctx.bot_id is the UNBOUND executor identity, not the bot",
      cap_d1["ctx"]["bot_id"] == "repo",
      f"got {cap_d1['ctx']['bot_id']}")
check("D1: ctx.rift_path is None (no bot rift inherited)",
      cap_d1["ctx"]["rift_path"] is None,
      f"got {cap_d1['ctx']['rift_path']}")
check("D1: ctx.policy is UNBOUND_POLICY, not the bot policy",
      cap_d1["ctx"]["policy"] == dict(serve.UNBOUND_POLICY),
      f"got {cap_d1['ctx']['policy']}")
check("D1: executor receives NO --rift / KYREX_FS_ROOT",
      rep_d1 and "--rift" not in rep_d1["argv"] and rep_d1["fs_root"] is None,
      f"argv={rep_d1 and rep_d1['argv']} fs_root={rep_d1 and rep_d1['fs_root']}")

print("\nD2. legitimate Bot session (session_key = bot identity 'octo') binds the Bot")
task_d2, cap_d2, rep_d2 = _submit_and_run_bot("octo")
check("D2: task row records bot identity 'octo'", task_d2["bot_id"] == "octo",
      f"got {task_d2['bot_id']}")
check("D2: task row records the bot's rift",
      task_d2["rift"] == "/tmp/rift-octo", f"got {task_d2['rift']}")
check("D2: ctx.bot_id is the BOT identity",
      cap_d2["ctx"]["bot_id"] == "octo", f"got {cap_d2['ctx']['bot_id']}")
check("D2: ctx.rift_path is the BOT rift",
      cap_d2["ctx"]["rift_path"] == "/tmp/rift-octo",
      f"got {cap_d2['ctx']['rift_path']}")
check("D2: ctx.policy is the BOT policy",
      cap_d2["ctx"]["policy"].get("fs:write") == 1,
      f"got {cap_d2['ctx']['policy']}")
check("D2: executor receives --rift <bot rift>",
      rep_d2 and "--rift" in rep_d2["argv"] and "/tmp/rift-octo" in rep_d2["argv"],
      f"argv={rep_d2 and rep_d2['argv']}")
check("D2: executor receives KYREX_FS_ROOT",
      rep_d2 and rep_d2["fs_root"] == "/tmp/rift-octo",
      f"fs_root={rep_d2 and rep_d2['fs_root']}")

# ── Test E: the web endpoint keeps opting out of bot resolution ────────────
print("\nE. web endpoint source guard: /api/task submits with resolve_bot=False")
_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
web_main_src = open(os.path.join(_TEST_DIR, "web", "backend", "main.py")).read()
accept_block = web_main_src.split('@app.post("/api/task")')[1].split("@app.get(")[0]
check("E: store.submit in /api/task carries resolve_bot=False",
      "resolve_bot=False" in accept_block,
      "endpoint lost the web-bot isolation opt-out!")

print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)