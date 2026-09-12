"""Bot conversation isolation — no shared engine history across conversations.

Regression for the contamination where a NEW Bot conversation ("hi") inherited
the PREVIOUS conversation's engine context (a TUI task's messages, files, task
state) and then reported "Task not verified complete — loop detected".

Root cause: the engine persisted/loaded its history from a workspace-relative
directory (``<cwd>/.px_sessions/main.json``). The cwd is the persistent Rift,
which every conversation bound to the same Bot shares — so conversation B's
engine booted from conversation A's on-disk history. ``conversation_id`` was
dropped at task submission and never reached the engine.

These assertions prove the fix end to end:

  1. ``serve.conversation_session_dir`` is deterministic and distinct per
     ``(owner, bot_id, conversation_id)``; it lives OUTSIDE any Rift and never
     embeds a workspace path.
  2. The REAL ``EngineSession`` spawn env carries a per-conversation
     ``KYREX_SESSION_DIR``; two conversations get two different dirs, and a
     stale inherited value is never leaked into a session without one.
  3. The writable-Bot path records ``conversation_id`` durably on the task row,
     and ``serve.run_task`` derives ``KYREX_SESSION_DIR`` from it (captured at
     the executor spawn boundary).
  4. Regression: conversation A runs a TUI task, conversation B sends "hi";
     B holds ONLY B's turn — none of A's messages/files/task state/completion —
     and A/B have distinct engine session directories despite the shared Rift.
  5. Concurrent two-conversation test for the same Bot: both complete, stay
     isolated, and never share a session directory.

Boundary: every execution assertion captures the EXACT values handed to the
engine/executor boundary (spawn env / session dir), never merely a 200.

Run: python3 test_bot_conversation_isolation.py
"""

import asyncio
import io
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

# Small task timeout so run_task's watchdog never outlives the test.
os.environ["KYREX_TASK_TIMEOUT"] = "3"
os.environ.setdefault("KYREX_DATA_DIR", tempfile.mkdtemp(prefix="kyrex-iso-tests-"))
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")
os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")

_BACKEND = os.path.dirname(os.path.abspath(__file__))            # web/backend
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))              # kyrex-cloud
sys.path.insert(0, _BACKEND)
sys.path.insert(0, _CLOUD)

import bots  # noqa: E402
import serve  # noqa: E402
import chat_service  # noqa: E402
import dev_bot  # noqa: E402
from task_store import CloudTaskStore  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name
          + ("" if cond else "  " + str(detail)))
    if not cond:
        failures.append(name)


def _rift_dir() -> str:
    return tempfile.mkdtemp(prefix="kyrex-iso-rift-")


def _reset():
    root = chat_service._chat_root()
    for p in list(root.rglob("*.json")) + list(root.rglob("*.json.tmp")):
        try:
            p.unlink()
        except OSError:
            pass
    bots.save_bots({})
    chat_service._engine_sessions.clear()


# ── A recording engine-session stand-in (real factory stays in place) ──
class _RecordingEngine:
    """Replaces EngineSession only. The REAL _get_engine_session still runs,
    so the value under test is the session dir the factory computes."""

    calls: list = []

    def __init__(self, workspace_path, provider_cfg, bot_cfg=None):
        bot_cfg = dict(bot_cfg or {})
        self.workspace = Path(workspace_path)
        self.bot_id = (bot_cfg.get("bot_id") or "").strip() or None
        self.session_dir = bot_cfg.get("session_dir")
        self.allowed_tools = chat_service._effective_caps(bot_cfg)
        self.surface_context = None
        self._closed = False
        self._proc = MagicMock()
        self._proc.poll.return_value = None  # alive
        _RecordingEngine.calls.append({
            "workspace": self.workspace,
            "bot_id": self.bot_id,
            "session_dir": self.session_dir,
        })

    def run_turn(self, text, on_token, cancel_check=None):
        on_token(f"answer<{text}>")
        return f"answer<{text}>", None

    def interrupt(self):
        pass

    def close(self):
        self._closed = True


async def _drain(agen):
    return [f async for f in agen]


def _terminal(frames):
    st = [f for f in frames if f.get("type") == "status"]
    return st[-1] if st else None


def _run_bot(user, conv_id, text):
    frames = asyncio.run(_drain(chat_service.stream_chat(user, conv_id, text)))
    return frames


# ═══════════════════════════════════════════════════════════════════════
print("\n0. KYREX_SESSION_DIR is server-generated and traversal-proof")
_base = serve.data_dir() / "engine_sessions"
_evil = serve.conversation_session_dir("../../etc", "../../root", "../../../../etc/passwd")
_evil_res = Path(_evil).resolve()
check("traversal input stays contained under the session root",
      str(_evil_res).startswith(str(_base.resolve())), _evil)
check("traversal input leaves no '..' segment",
      not any(seg == ".." for seg in Path(_evil).parts), _evil)
check("traversal input cannot escape the data root",
      str(_evil_res).startswith(str(serve.data_dir().resolve())), _evil)
_abs = Path(serve.conversation_session_dir("/etc", "/root", "/etc/shadow")).resolve()
check("absolute-path inputs stay contained under the session root",
      str(_abs).startswith(str(_base.resolve())), _abs)

# ═══════════════════════════════════════════════════════════════════════
print("\n1. session dir is deterministic and distinct per conversation")
d1 = serve.conversation_session_dir("alice", "devbot", "conv-A")
d1b = serve.conversation_session_dir("alice", "devbot", "conv-A")
d2 = serve.conversation_session_dir("alice", "devbot", "conv-B")
d_owner = serve.conversation_session_dir("bob", "devbot", "conv-A")
d_bot = serve.conversation_session_dir("alice", "otherbot", "conv-A")
check("same key -> same dir (stable across retries)", d1 == d1b)
check("different conversation -> different dir", d1 != d2)
check("different owner -> different dir", d1 != d_owner)
check("different bot -> different dir", d1 != d_bot)
check("session dir lives under the data root, not a Rift",
      str(serve.data_dir()) in d1, d1)
_probe_rift = _rift_dir()
check("session dir is not derived from / inside the Rift",
      _probe_rift not in d1 and d1 != _probe_rift
      and "engine_sessions" in d1, d1)

# ═══════════════════════════════════════════════════════════════════════
print("\n2. the REAL EngineSession spawn env carries KYREX_SESSION_DIR")
cfgs = {}

def _capture_spawn(workspace, provider_cfg, bot_cfg=None):
    def fake_popen(*a, **k):
        cfgs["env"] = dict(k.get("env") or {})
        cfgs["cwd"] = k.get("cwd")
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.stdout = io.StringIO()
        proc.stderr = io.StringIO()
        proc.stdin = io.StringIO()
        return proc
    with patch("subprocess.Popen", side_effect=fake_popen), \
         patch.object(chat_service, "ENGINE_HANDSHAKE_TIMEOUT", 0.2):
        try:
            chat_service.EngineSession(Path(workspace), provider_cfg, bot_cfg)
        except chat_service.EngineSessionError:
            pass


rift = _rift_dir()
cfg = chat_service._resolve_provider()
_capture_spawn(rift, cfg, {"bot_id": "devbot", "session_dir": d1})
check("KYREX_SESSION_DIR == this conversation's dir",
      cfgs["env"].get("KYREX_SESSION_DIR") == d1, cfgs["env"].get("KYREX_SESSION_DIR"))
check("engine still runs in the Rift (workspace awareness intact)",
      cfgs["cwd"] == rift, cfgs["cwd"])
_capture_spawn(rift, cfg, {"bot_id": "devbot", "session_dir": d2})
check("second conversation -> different spawn session dir",
      cfgs["env"].get("KYREX_SESSION_DIR") == d2
      and cfgs["env"].get("KYREX_SESSION_DIR") != d1)
# A session with no explicit dir must NOT inherit a stale one.
os.environ["KYREX_SESSION_DIR"] = "/tmp/stale-inherited-session"
_capture_spawn(rift, cfg, None)
check("no explicit dir -> stale KYREX_SESSION_DIR is dropped",
      "KYREX_SESSION_DIR" not in cfgs["env"], cfgs["env"].get("KYREX_SESSION_DIR"))
os.environ.pop("KYREX_SESSION_DIR", None)

# ═══════════════════════════════════════════════════════════════════════
print("\n3. writable-Bot task records conversation_id; run_task derives the dir")
_reset()
wr_rif = _rift_dir()
bots.add_bot("devbot", "Dev", "openai:gpt-test", wr_rif, status="running",
             owner="alice", policy={"fs:write": 1})
bot = bots.get_bot("devbot")
store = CloudTaskStore(os.path.join(os.environ["KYREX_DATA_DIR"], "iso.sqlite"))
tA = dev_bot.submit_bot_task("alice", bot, "fix the TUI paste bug",
                             store=store, conversation_id="conv-A")
tB = dev_bot.submit_bot_task("alice", bot, "hi", store=store,
                             conversation_id="conv-B")
rowA, rowB = store.get(tA), store.get(tB)
check("conversation_id recorded on the task row",
      rowA.get("conversation_id") == "conv-A"
      and rowB.get("conversation_id") == "conv-B",
      (rowA.get("conversation_id"), rowB.get("conversation_id")))
check("session_key stays the per-Bot serialisation key",
      rowA.get("session_key") == "devbot" == rowB.get("session_key"))

captured = {}

class _FakeProc:
    def __init__(self):
        self.stdout = iter([])
        self.stderr = io.StringIO("")
        self.stdin = io.StringIO()
        self.returncode = 0
    def poll(self): return 0
    def wait(self, timeout=None): return 0
    def kill(self): pass

def _capture_exec(*a, **k):
    captured["env"] = dict(k.get("env") or {})
    return _FakeProc()

with patch("serve.subprocess.Popen", side_effect=_capture_exec):
    serve.run_task("alice", None, "hi", session_key="devbot",
                   send=lambda c, t: "mid", edit=lambda *a: None,
                   conversation_id="conv-B")
expB = serve.conversation_session_dir("alice", "devbot", "conv-B")
got = captured["env"].get("KYREX_SESSION_DIR")
check("run_task sets KYREX_SESSION_DIR for the conversation", got == expB, got)
check("run_task session dir is not the Rift", got != wr_rif and wr_rif not in (got or ""))

# retry / resume: the SAME conversation resolves the SAME directory.
with patch("serve.subprocess.Popen", side_effect=_capture_exec):
    serve.run_task("alice", None, "again", session_key="devbot",
                   send=lambda c, t: "mid", edit=lambda *a: None,
                   conversation_id="conv-B")
got2 = captured["env"].get("KYREX_SESSION_DIR")
check("retry/resume reuses the SAME session dir",
      got2 == got == expB, (got, got2, expB))
check("a different conversation resolves a different dir",
      serve.conversation_session_dir("alice", "devbot", "conv-C") != expB)

# ── 3b. the executor → engine hop preserves KYREX_SESSION_DIR ──
import headless_agent  # noqa: E402
engine_env = {}
_def_session = os.environ["KYREX_SESSION_DIR"] = expB


class _HelloProc:
    def __init__(self):
        self.stdout = iter(['{"type":"phase","value":"IDLE"}\n'])
        self.stderr = io.StringIO("")
        self.stdin = io.StringIO()
        self.returncode = 0
    def poll(self): return None
    def wait(self, timeout=None): return 0
    def kill(self): pass


def _capture_engine_popen(*a, **k):
    engine_env.update(k.get("env") or {})
    return _HelloProc()


with patch("headless_agent.subprocess.Popen", side_effect=_capture_engine_popen):
    _agent = headless_agent.HeadlessAgent(Path("/tmp/x/core_bridge.py"), Path(wr_rif))
    _agent.start("hi")
os.environ.pop("KYREX_SESSION_DIR", None)

check("engine subprocess inherits KYREX_SESSION_DIR (end-to-end env hop)",
      engine_env.get("KYREX_SESSION_DIR") == expB, engine_env.get("KYREX_SESSION_DIR"))
check("engine still runs with the Rift as cwd (workspace awareness intact)",
      engine_env.get("WORKSPACE_ROOT") == str(wr_rif), engine_env.get("WORKSPACE_ROOT"))

# ═══════════════════════════════════════════════════════════════════════
print("\n4. regression: A's TUI task must not leak into B's 'hi'")
_reset()
chat_service._engine_sessions.clear()
_RecordingEngine.calls.clear()
shared_rift = _rift_dir()
bots.add_bot("devbot", "Dev", "openai:gpt-test", shared_rift, status="running",
             owner="alice", policy={})  # read-only Bot -> engine session path
convA = chat_service.create_conversation("alice", bot_id="devbot")
convB = chat_service.create_conversation("alice", bot_id="devbot")
idA, idB = convA["conversation_id"], convB["conversation_id"]

with patch.object(chat_service, "EngineSession", _RecordingEngine):
    rA = _run_bot("alice", idA, "fix the TUI paste bug and commit uncommitted files")
    # Simulate A having written a session artefact into its own session dir.
    Path(_RecordingEngine.calls[0]["session_dir"]).mkdir(parents=True, exist_ok=True)
    (Path(_RecordingEngine.calls[0]["session_dir"]) / "A_history.json").write_text("A")
    rB = _run_bot("alice", idB, "hi")

tA, tB = _terminal(rA), _terminal(rB)
check("A completed", tA and tA["status"] == "complete", tA)
check("B completed", tB and tB["status"] == "complete", tB)

cA = chat_service.get_conversation("alice", idA)
cB = chat_service.get_conversation("alice", idB)
bconv_text = " ".join(m.get("content", "") for m in cB["messages"])
check("B's stored messages are ONLY B's turn",
      [m["role"] for m in cB["messages"]] == ["user", "assistant"]
      and cB["messages"][0]["content"] == "hi", cB["messages"])
check("B contains none of A's content (TUI / uncommitted / task text)",
      "TUI" not in bconv_text and "uncommitted" not in bconv_text
      and "paste bug" not in bconv_text, bconv_text)
check("B's completion text is not A's completion text",
      tB["content"] == "answer<hi>", tB.get("content"))

dirA = _RecordingEngine.calls[0]["session_dir"]
dirB = _RecordingEngine.calls[1]["session_dir"]
check("both conversations ran in the SAME shared Rift",
      _RecordingEngine.calls[0]["workspace"] == _RecordingEngine.calls[1]["workspace"],
      (_RecordingEngine.calls[0]["workspace"], _RecordingEngine.calls[1]["workspace"]))
check("but on DIFFERENT engine session dirs (the fix)", dirA != dirB, (dirA, dirB))
check("A's session file is absent from B's session dir",
      not (Path(dirB) / "A_history.json").exists())
check("A's session dir holds A's file (sanity)",
      (Path(dirA) / "A_history.json").exists())

# ═══════════════════════════════════════════════════════════════════════
print("\n5. concurrent two-conversation isolation for the SAME Bot")
_reset()
chat_service._engine_sessions.clear()
_RecordingEngine.calls.clear()
bots.add_bot("devbot", "Dev", "openai:gpt-test", shared_rift, status="running",
             owner="alice", policy={})
c1 = chat_service.create_conversation("alice", bot_id="devbot")["conversation_id"]
c2 = chat_service.create_conversation("alice", bot_id="devbot")["conversation_id"]

barrier = threading.Barrier(2)
results = {}
errors = []

def _worker(cid, text):
    try:
        barrier.wait(timeout=5)
        results[cid] = _run_bot("alice", cid, text)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{type(exc).__name__}: {exc}")

with patch.object(chat_service, "EngineSession", _RecordingEngine):
    th1 = threading.Thread(target=_worker, args=(c1, "task one"))
    th2 = threading.Thread(target=_worker, args=(c2, "hi"))
    th1.start(); th2.start(); th1.join(10); th2.join(10)

check("both concurrent turns completed without error", not errors, errors)
_t1 = _terminal(results.get(c1) or []) or {}
_t2 = _terminal(results.get(c2) or []) or {}
check("both returned a terminal frame",
      _t1.get("status") == "complete" and _t2.get("status") == "complete",
      (_t1, _t2))

sdirs = [c["session_dir"] for c in _RecordingEngine.calls]
check("concurrent conversations used DISTINCT session dirs",
      len(set(sdirs)) == len(sdirs) == 2, sdirs)
check("concurrent conversations recorded distinct conversation_id keys",
      len({(c["bot_id"], c["session_dir"]) for c in _RecordingEngine.calls}) == 2)

m1 = " ".join(m.get("content", "") for m in
              chat_service.get_conversation("alice", c1)["messages"])
m2 = " ".join(m.get("content", "") for m in
              chat_service.get_conversation("alice", c2)["messages"])
check("concurrent stored messages do not cross",
      "task one" in m1 and "task one" not in m2 and "hi" in m2 and "hi" not in m1,
      (m1, m2))

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("ALL PASSED — Bot conversations are isolated by (owner, bot_id, conversation_id)")
