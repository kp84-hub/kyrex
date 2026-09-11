"""Focused regression tests for the repo-connected Chat turn lifecycle.

Two failure modes fixed in chat_service:

  A. EngineSession.run_turn must terminate on an explicit engine ``error``
     frame or on a frame-less engine failure (unhandled bridge exception
     printed to stderr) — a terminal error must never wait out the long
     outer turn timeout for a chat_done that will never come.

  B. stream_chat finalization must be cancellation-safe: closing the async
     generator mid-turn (client disconnect) must finalize promptly, bound
     the worker join, request engine cancellation, never yield a terminal
     frame after close has begun, and leave no asyncio finalizer task
     (async_generator_athrow) pending behind a 30s join.

Plus one retained acceptance test: a repo-connected turn driven by the REAL
core_bridge.py against an inline mock OpenAI-compatible SSE provider must
reach the provider, stream tokens, complete through the chat_done contract,
and terminate with status "complete".

No real LLM is contacted anywhere in this file.
"""

import asyncio
import json
import os
import queue as _queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-chat-failure-tests")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))
REPO_ROOT = Path(_HERE).parent.parent.parent          # repo root
ENGINE_DIR = REPO_ROOT / "kyrex_engine"
sys.path.insert(0, str(ENGINE_DIR))

import chat_service  # noqa: E402

PROOF = "KYREX_CHAT_PROOF_MARKER_12345"


# ── helpers ────────────────────────────────────────────────────────

def _make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "testws"
    ws.mkdir()
    (ws / "PROOF.txt").write_text(PROOF)
    return ws


def _reset():
    root = chat_service._chat_root()
    for p in root.rglob("*.json"):
        p.unlink()
    for p in root.rglob("*.json.tmp"):
        p.unlink()
    chat_service.close_all_engine_sessions()


def setup_function():
    _reset()


def teardown_function():
    _reset()


@pytest.fixture(autouse=True)
def _close_sessions():
    yield
    chat_service.close_all_engine_sessions()


def _bare_session() -> chat_service.EngineSession:
    """An EngineSession without a real subprocess: enough plumbing for
    run_turn unit tests (frames queue, locks, stderr tail, fake send/proc)."""
    sess = object.__new__(chat_service.EngineSession)
    sess._closed = False
    sess._turn_lock = threading.Lock()
    sess._frames = _queue.Queue()
    sess._stdin_lock = threading.Lock()
    sess.stderr_tail = []
    sess._stderr_lock = threading.Lock()
    sess.denied_requests = []
    # Per-turn Kyrex Chat surface context (None for these non-Bot fixtures).
    sess.surface_context = None

    class _FakeProc:
        def poll(self):
            return None

    sess._proc = _FakeProc()
    sess._send = lambda payload: None
    return sess


# ── A. run_turn terminal error handling ────────────────────────────

def test_run_turn_error_frame_terminates_promptly(monkeypatch):
    """An explicit engine ``error`` frame must terminate the turn immediately:
    the caller gets the error text instead of waiting for a chat_done that the
    failed engine may never emit (and instead of the long outer timeout)."""
    monkeypatch.setattr(chat_service, "ENGINE_TURN_TIMEOUT", 30.0)
    sess = _bare_session()
    sess._frames.put({"type": "error", "content": "engine exploded: OOM"})
    tokens = []
    started = time.monotonic()
    final, err = sess.run_turn("hi", tokens.append)
    elapsed = time.monotonic() - started

    assert err == "engine exploded: OOM"
    assert elapsed < 5, f"error frame did not terminate the turn promptly: {elapsed:.2f}s"
    assert final == ""


def test_run_turn_frame_less_stderr_failure_terminates_promptly(monkeypatch):
    """A frame-less engine failure (unhandled bridge exception printed to
    stderr; no chat_done / phase IDLE frame ever follows) must terminate the
    turn promptly with an actionable error instead of waiting out the long
    outer timeout."""
    monkeypatch.setattr(chat_service, "ENGINE_TURN_TIMEOUT", 30.0)
    sess = _bare_session()
    tokens = []

    def _fail_after_delay():
        time.sleep(0.05)
        with sess._stderr_lock:
            sess.stderr_tail.append("Traceback (most recent call last):")
            sess.stderr_tail.append('  File "/k/bridge.py", line 42, in listen_to_go')
            sess.stderr_tail.append("RuntimeError: engine hit an unhandled provider bug")

    threading.Thread(target=_fail_after_delay, daemon=True).start()

    started = time.monotonic()
    final, err = sess.run_turn("hi", tokens.append)
    elapsed = time.monotonic() - started

    assert err is not None and "engine hit an unhandled provider bug" in err, err
    assert elapsed < 5, f"frame-less failure did not terminate promptly: {elapsed:.2f}s"
    assert final == ""


def test_run_turn_still_completes_on_normal_chat_done():
    """Regressed behavior (preserved): a healthy turn that emits tokens then
    chat_done + phase IDLE still returns its final content with no error."""
    sess = _bare_session()
    sess._frames.put({"type": "token", "content": "Hel"})
    sess._frames.put({"type": "token", "content": "lo"})
    sess._frames.put({"type": "chat_done", "content": "Hello", "reasoning": ""})
    sess._frames.put({"type": "phase", "value": "IDLE"})
    tokens = []
    final, err = sess.run_turn("hi", tokens.append)
    assert err is None
    assert tokens == ["Hel", "lo"]
    assert final == "Hello"


# ── B. stream_chat cancellation-safe finalization ──────────────────

class _StallSession:
    """Duck-typed engine session that streams one token, then stalls until
    either cancel_check() flips or interrupt() is called (models a hung
    engine bridge that only stops when interrupted)."""

    def __init__(self, workspace):
        self.workspace = workspace
        self.interrupts = 0
        self.stop = threading.Event()

    def run_turn(self, text, on_token, cancel_check=None):
        try:
            on_token("partial")
            while not cancel_check() and not self.stop.is_set():
                time.sleep(0.005)
            return "partial", None
        finally:
            pass

    def interrupt(self):
        self.interrupts += 1
        self.stop.set()


def _repo_stream_env(monkeypatch, tmp_path, sess):
    ws = _make_workspace(tmp_path)
    monkeypatch.setenv("KYREX_CHAT_WORKSPACES", json.dumps({"testws": str(ws)}))
    monkeypatch.delenv("KYREX_CHAT_WORKSPACE", raising=False)
    monkeypatch.setenv("KYREX_PROVIDER", "openai")
    monkeypatch.setenv("KYREX_MODEL", "gpt-test")
    monkeypatch.setenv("KYREX_API_KEY", "sk-test")
    # _get_engine_session gained the optional bot_cfg (Bot-aware execution);
    # the failure-path sessions are never Bot-bound, but stream_chat passes
    # bot_cfg=None positionally on every engine-path turn.
    monkeypatch.setattr(chat_service, "_get_engine_session",
                        lambda u, c, w, bot_cfg=None: sess)
    return ws


def test_stream_chat_closed_mid_turn_finalizes_promptly(monkeypatch, tmp_path):
    """Closing stream_chat mid-turn (client disconnect) must finalize
    promptly: it requests engine cancellation, bounds the worker join, and
    does NOT raise 'async generator ignored GeneratorExit' from a post-close
    terminal yield. The old behavior joined the worker up to 30s in the
    finally and then yielded — leaving the async_generator_athrow finalizer
    task pending."""
    sess = _StallSession(tmp_path)
    _repo_stream_env(monkeypatch, tmp_path, sess)

    async def main():
        gen = chat_service.stream_chat("disconnect", "", "hi", workspace_id="testws")
        agen = gen.__aiter__()
        first = await agen.__anext__()
        assert first["type"] == "conversation"
        second = await agen.__anext__()
        assert second["type"] == "delta"
        started = time.monotonic()
        await gen.aclose()          # must not raise, must not hang
        return time.monotonic() - started

    elapsed = asyncio.run(main())

    assert elapsed < 5, f"aclose did not finalize promptly: {elapsed:.2f}s"
    assert sess.interrupts >= 1, "disconnect did not request engine cancellation"
    assert sess.stop.is_set(), "engine turn was not told to stop on disconnect"


def test_stream_chat_cancel_emits_cancelled_and_retires_worker(monkeypatch, tmp_path):
    """Cooperative /api/chat/cancel on a repo turn: the stream emits the
    cancelled terminal, carries the partial content, persists NO assistant
    message, and the worker retires (bounded lifecycle)."""
    sess = _StallSession(tmp_path)
    _repo_stream_env(monkeypatch, tmp_path, sess)

    async def main():
        cancel = asyncio.Event()
        frames = []
        async for f in chat_service.stream_chat(
                "canceluser", "", "hi", cancel, workspace_id="testws"):
            frames.append(f)
            if f["type"] == "delta":
                cancel.set()
        return frames

    frames = asyncio.run(main())

    terminal = [f for f in frames if f["type"] == "status"][-1]
    assert terminal["status"] == "cancelled", frames
    assert terminal["content"] == "partial"
    conv_id = frames[0]["conversation_id"]
    conv = chat_service.get_conversation("canceluser", conv_id)
    roles = [m["role"] for m in conv["messages"]]
    assert roles == ["user"], "cancelled turn must not persist an assistant message"
    assert sess.interrupts >= 1
    # Worker is retired within a bounded window (no orphaned chat-* thread).
    time.sleep(0.2)
    leftovers = [t for t in threading.enumerate()
                 if t.name.startswith("chat-") and t.is_alive()]
    assert leftovers == [], f"orphaned worker threads: {[t.name for t in leftovers]}"


def test_stream_chat_clean_completion_does_not_interrupt_idle_engine(monkeypatch, tmp_path):
    """A cleanly-completed repo turn must NOT send a spurious interrupt to the
    now-idle engine (regression guard for the finally's cancel policy)."""
    class _QuickSession:
        workspace = tmp_path

        def __init__(self):
            self.interrupts = 0

        def run_turn(self, text, on_token, cancel_check=None):
            on_token("done")
            return "done", None

        def interrupt(self):
            self.interrupts += 1

    sess = _QuickSession()
    _repo_stream_env(monkeypatch, tmp_path, sess)

    async def main():
        frames = []
        async for f in chat_service.stream_chat(
                "cleanuser", "", "hi", workspace_id="testws"):
            frames.append(f)
        return frames

    frames = asyncio.run(main())
    terminal = [f for f in frames if f["type"] == "status"][-1]
    assert terminal["status"] == "complete"
    assert terminal["content"] == "done"
    assert sess.interrupts == 0, "clean completion must not interrupt the idle engine"


# ── retained acceptance: real bridge + mock provider, repo-connected ─

class _MockSSEHandler(BaseHTTPRequestHandler):
    """OpenAI-compatible streaming endpoint: plain text chunks, then usage,
    then [DONE]. Mirrors scratch_mock_provider.py's text mode."""

    requests = 0

    def do_POST(self):
        type(self).requests += 1
        length = int(self.headers.get("content-length", 0))
        if length:
            self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def sse(obj):
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()

        rid = "chatcmpl-failurepaths"
        created = 1700000000
        for piece in ["Hello", " from", " the", " mock", " provider."]:
            sse({"id": rid, "object": "chat.completion.chunk", "created": created,
                 "model": "mock-model",
                 "choices": [{"index": 0, "delta": {"content": piece},
                              "finish_reason": None}]})
        sse({"id": rid, "object": "chat.completion.chunk", "created": created,
             "model": "mock-model", "choices": [],
             "usage": {"prompt_tokens": 11, "completion_tokens": 7}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, fmt, *args):
        pass


class _MockProviderServer:
    def __init__(self):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _MockSSEHandler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}/v1"

    @property
    def requests(self) -> int:
        return _MockSSEHandler.requests

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()


def test_repo_connected_successful_turn_with_mock_provider(monkeypatch, tmp_path):
    """ACCEPTANCE (retained behavior): a repo-connected turn driven by the
    REAL core_bridge.py against an inline mock OpenAI-compatible SSE provider
    must reach the provider, stream tokens, complete through the chat_done
    contract, and yield a 'complete' terminal status."""
    server = _MockProviderServer()
    try:
        ws = _make_workspace(tmp_path)
        monkeypatch.setenv("KYREX_PROVIDER", "openai")
        monkeypatch.setenv("KYREX_MODEL", "mock-model")
        monkeypatch.setenv("KYREX_API_KEY", "sk-mock")
        monkeypatch.setenv("KYREX_BASE_URL", server.base_url)
        monkeypatch.setenv("OPENAI_BASE_URL", server.base_url)
        monkeypatch.setenv("KYREX_CHAT_WORKSPACES",
                           json.dumps({"testws": str(ws.resolve())}))
        monkeypatch.delenv("KYREX_CHAT_WORKSPACE", raising=False)
        monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
        monkeypatch.delenv("PROJECT_SOURCE_ROOT", raising=False)

        async def main():
            frames = []
            async for f in chat_service.stream_chat(
                    "e2euser", "", "hi", workspace_id="testws"):
                frames.append(f)
            return frames

        frames = asyncio.run(main())
    finally:
        server.stop()

    terminal = [f for f in frames if f["type"] == "status"][-1]
    assert terminal["status"] == "complete", frames
    final = terminal["content"]
    assert "Hello" in final, final
    assert any(f["type"] == "delta" for f in frames)
    assert server.requests >= 1, "the engine never reached the provider"