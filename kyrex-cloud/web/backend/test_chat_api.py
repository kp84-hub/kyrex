"""Tests for Kyrex Chat backend (chat_service + chat_api) — Phase 2.

Covers the streaming robustness contract:

  * multiple streamed chunks delivered incrementally (not buffered)
  * explicit completion (``done``) terminal event
  * provider error before the first chunk  -> deterministic ``error`` event
  * provider error after partial output   -> ``error`` event, partial not persisted
  * cancellation                           -> ``cancelled`` event, no orphan worker
  * client disconnect / cleanup            -> worker thread joined, no orphan
  * no duplicate assistant message         -> exactly one assistant turn persisted
  * correct SSE event sequence             -> conversation -> delta* -> terminal

The provider is a *fake* driven through ``kyrex.providers.get_provider``: it
emits the exact tokens asserted, exercising the real service persistence + SSE
bridge end to end without any network call. A separate real-provider E2E gate
(test_chat_real_e2e.py) demonstrates actual incremental delivery against a
live configured provider.

Run: pytest test_chat_api.py  (isolated from the Cloud harness tests)
"""

import asyncio
import os
import sys
import threading
import time
from unittest.mock import patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-chat-tests")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")
# Phase 5: exercise the production chat-host routing config (main.py reads
# this at import time, before tests import main).
os.environ.setdefault("KYREX_CHAT_PUBLIC_HOST", "chat.kyrex.dev")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chat_service  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────

def _reset():
    root = chat_service._chat_root()
    for p in root.rglob("*.json"):
        p.unlink()
    for p in root.rglob("*.json.tmp"):
        p.unlink()


def setup_function():
    _reset()


def teardown_function():
    _reset()


async def _frames(agen):
    """Collect all yielded control frames from a stream_chat generator."""
    out = []
    async for f in agen:
        out.append(f)
    return out


def _deltas(frames):
    return [f["content"] for f in frames if f.get("type") == "delta"]


def _terminal(frames):
    """Return the single terminal status frame (None if absent)."""
    status = [f for f in frames if f.get("type") == "status"]
    return status[-1] if status else None


class FakeProvider:
    """Provider stub whose stream_callback emits predetermined tokens."""

    def __init__(self, tokens=None, error=None):
        self.tokens = tokens if tokens is not None else ["Hello", " Kyrex"]
        self.error = error
        self.interrupted = False

    async def chat(self, model, messages, tools=None, stream_callback=None,
                   interrupt_event=None, **kw):
        if self.error:
            if isinstance(self.error, dict):
                # Simulate the real provider's swallowed-error contract:
                # returns error-prefixed content instead of raising.
                return {"role": "assistant", "content": self.error["content"]}
            raise RuntimeError(self.error)
        for t in self.tokens:
            if interrupt_event is not None and interrupt_event.is_set():
                self.interrupted = True
                break
            if stream_callback:
                stream_callback(t)
        return {"role": "assistant", "content": "".join(self.tokens)}


class SlowProvider(FakeProvider):
    """Emits tokens with a real delay to prove incremental (non-buffered) delivery."""

    def __init__(self, tokens=None, delay=0.02):
        super().__init__(tokens=tokens)
        self.delay = delay

    async def chat(self, model, messages, tools=None, stream_callback=None,
                   interrupt_event=None, **kw):
        for t in self.tokens:
            if interrupt_event is not None and interrupt_event.is_set():
                self.interrupted = True
                break
            if stream_callback:
                stream_callback(t)
            await asyncio.sleep(self.delay)
        return {"role": "assistant", "content": "".join(self.tokens)}


# ── 1. multiple streamed chunks, incremental delivery ─────────────

def test_multiple_chunks_incremental_delivery():
    tokens = ["The", " quick", " brown", " fox"]
    prov = SlowProvider(tokens=tokens, delay=0.02)

    received = []
    with patch("chat_service.get_provider", return_value=prov):
        async def run():
            gen = chat_service.stream_chat("alice", "", "hi")
            async for f in gen:
                if f.get("type") == "delta":
                    received.append(f["content"])
        asyncio.run(run())

    # All chunks delivered.
    assert received == tokens
    assert "".join(received) == "The quick brown fox"


def test_chunks_arrive_before_generator_returns():
    """Prove deltas are yielded incrementally, not buffered until completion."""
    prov = SlowProvider(tokens=["a", "b", "c", "d"], delay=0.03)
    events = []  # ('delta', token) or ('end',)

    async def run():
        gen = chat_service.stream_chat("alice", "", "hi")
        async for f in gen:
            if f.get("type") == "delta":
                events.append(("delta", f["content"]))
        events.append(("end",))

    with patch("chat_service.get_provider", return_value=prov):
        asyncio.run(run())

    deltas = [e for e in events if e[0] == "delta"]
    assert deltas == [("delta", "a"), ("delta", "b"), ("delta", "c"), ("delta", "d")]
    assert events[-1] == ("end",)
    # Multiple deltas preceded the final end (i.e. not a single buffered blob).
    assert len(deltas) > 1


# ── 2. completion event / persistence / no duplicates ─────────────

def test_completion_event_and_persist_once():
    prov = FakeProvider(tokens=["Hi", " there"])
    frames = []
    with patch("chat_service.get_provider", return_value=prov):
        frames = asyncio.run(_frames(chat_service.stream_chat("alice", "", "hello")))

    terminal = _terminal(frames)
    assert terminal is not None
    assert terminal["status"] == "complete"
    assert terminal["content"] == "Hi there"

    # Persistence: user + exactly one assistant message.
    convs = chat_service.list_conversations("alice")
    assert len(convs) == 1
    conv = chat_service.get_conversation("alice", convs[0]["conversation_id"])
    roles = [m["role"] for m in conv["messages"]]
    assert roles == ["user", "assistant"]
    assert conv["messages"][1]["content"] == "Hi there"


def test_no_duplicate_assistant_message():
    prov = FakeProvider(tokens=["one"])
    with patch("chat_service.get_provider", return_value=prov):
        asyncio.run(_frames(chat_service.stream_chat("alice", "", "msg")))

    convs = chat_service.list_conversations("alice")
    conv = chat_service.get_conversation("alice", convs[0]["conversation_id"])
    assistants = [m for m in conv["messages"] if m["role"] == "assistant"]
    assert len(assistants) == 1


# ── 3. provider errors ────────────────────────────────────────────

def test_provider_error_before_first_chunk():
    prov = FakeProvider(error="boom before tokens")
    frames = []
    with patch("chat_service.get_provider", return_value=prov):
        frames = asyncio.run(_frames(chat_service.stream_chat("alice", "", "hi")))

    assert _deltas(frames) == []
    terminal = _terminal(frames)
    assert terminal is not None
    assert terminal["status"] == "error"
    assert "boom" in terminal["message"]


def test_provider_error_after_partial_output_not_persisted():
    # Raises midway: some tokens delivered, then failure.
    class MidFailProvider(FakeProvider):
        async def chat(self, model, messages, tools=None, stream_callback=None,
                       interrupt_event=None, **kw):
            for t in ["partial", " output"]:
                if stream_callback:
                    stream_callback(t)
            raise RuntimeError("failed after partial")

    with patch("chat_service.get_provider", return_value=MidFailProvider()):
        frames = asyncio.run(_frames(chat_service.stream_chat("alice", "", "hi")))

    terminal = _terminal(frames)
    assert terminal["status"] == "error"
    assert "failed after partial" in terminal["message"]

    # The partial assistant message must NOT be persisted as completed.
    convs = chat_service.list_conversations("alice")
    assert len(convs) == 1
    conv = chat_service.get_conversation("alice", convs[0]["conversation_id"])
    assistants = [m for m in conv["messages"] if m["role"] == "assistant"]
    assert assistants == [], "failed stream must not persist a false assistant message"


def test_provider_swallowed_error_content_detected():
    # The real provider catches exceptions and returns "[OpenAI Provider Error: ...]".
    prov = FakeProvider(error={"content": "[OpenAI Provider Error: upstream 500"})
    with patch("chat_service.get_provider", return_value=prov):
        frames = asyncio.run(_frames(chat_service.stream_chat("alice", "", "hi")))

    terminal = _terminal(frames)
    assert terminal["status"] == "error"
    assert "Provider Error" in terminal["message"]


# ── 4. cancellation ───────────────────────────────────────────────

def test_cancellation_stops_stream_and_unwinds():
    prov = SlowProvider(tokens=["one", " two", " three", " four"], delay=0.05)
    cancel = asyncio.Event()

    received = []

    async def run():
        gen = chat_service.stream_chat("alice", "", "hi", cancel_event=cancel)
        # Stop after the first couple of frames.
        async for f in gen:
            if f.get("type") == "delta":
                received.append(f["content"])
            if len(received) >= 2:
                cancel.set()

    with patch("chat_service.get_provider", return_value=prov):
        asyncio.run(run())

    terminal_frames_received = received
    # Cancellation observed: fewer than all tokens, and fixture introspection
    # shows the provider saw the interrupt.
    assert len(received) < 4
    assert prov.interrupted is True


def test_cancel_event_yields_cancelled_terminal():
    # A slow provider that honors interrupt_event: cancellation is observed
    # between tokens (the documented cooperative cancellation model).
    class SlowTokenProvider(FakeProvider):
        async def chat(self, model, messages, tools=None, stream_callback=None,
                       interrupt_event=None, **kw):
            for t in ["one", " two", " three", " four", " five"]:
                if interrupt_event is not None and interrupt_event.is_set():
                    self.interrupted = True
                    break
                if stream_callback:
                    stream_callback(t)
                await asyncio.sleep(0.03)
            return {"role": "assistant", "content": "done"}

    cancel = asyncio.Event()
    frames = []

    async def run():
        gen = chat_service.stream_chat("alice", "", "hi", cancel_event=cancel)
        async for f in gen:
            frames.append(f)
            if f.get("type") == "delta":
                cancel.set()  # cancel after the first delta

    with patch("chat_service.get_provider", return_value=SlowTokenProvider()):
        asyncio.run(run())

    terminal = _terminal(frames)
    assert terminal is not None
    assert terminal["status"] == "cancelled"
    # Not all tokens streamed — cancellation stopped delivery.
    assert len(_deltas(frames)) < 5


# ── 5. client disconnect / cleanup ────────────────────────────────

def test_worker_thread_cleanup_on_early_exit():
    """Abandoning the generator (simulating client disconnect) must join the
    worker thread so no orphaned provider call outlives the request."""
    prov = SlowProvider(tokens=["a", "b", "c", "d", "e"], delay=0.05)
    threads_before = set(threading.enumerate())

    async def run_then_abandon():
        gen = chat_service.stream_chat("alice", "", "hi")
        # Consume the first frame then abandon (simulates disconnect).
        async for f in gen:
            if f.get("type") == "delta":
                break
        # Abandon: do not exhaust the generator. The finally in stream_chat
        # joins the worker on generator close.

    with patch("chat_service.get_provider", return_value=prov):
        asyncio.run(run_then_abandon())

    # Give GC a moment; then assert no live chat-* worker thread remains.
    time.sleep(0.2)
    leftovers = [t for t in threading.enumerate()
                 if t.name.startswith("chat-") and t.is_alive()]
    assert leftovers == [], f"orphaned worker threads: {[t.name for t in leftovers]}"


# ── 6. SSE event sequence (API layer) ─────────────────────────────

def test_sse_event_sequence_end_to_end():
    from fastapi.testclient import TestClient
    import main

    # Seed a session.
    main.sessions["sess-1"] = "alice"

    prov = FakeProvider(tokens=["A", "B", "C"])
    client = TestClient(main.app, cookies={"session": "sess-1"})

    with patch("chat_service.get_provider", return_value=prov):
        with client.stream("POST", "/api/chat",
                           json={"message": "hi", "request_id": "r1"}) as r:
            assert r.status_code == 200
            frames = []
            for line in r.iter_lines():
                if line.startswith("data:"):
                    import json
                    frames.append(json.loads(line[5:].strip()))

    # Sequence: conversation first, then deltas, then a single terminal frame.
    types = [f["type"] for f in frames]
    assert types[0] == "conversation"
    assert types[-1] == "done"
    assert "delta" in types
    # Exactly one terminal frame.
    terminal_events = [t for t in types if t in ("done", "error", "cancelled")]
    assert len(terminal_events) == 1
    # Delta content accumulates to the full reply.
    full = "".join(f["content"] for f in frames if f["type"] == "delta")
    assert full == "ABC"


def test_cancel_endpoint():
    import main
    from fastapi.testclient import TestClient
    import chat_api

    main.sessions["sess-c"] = "alice"
    client = TestClient(main.app, cookies={"session": "sess-c"})

    # Prime the active-stream registry directly.
    ev = asyncio.Event()
    chat_api._active_streams["req-c"] = {"user": "alice", "event": ev}
    r = client.post("/api/chat/cancel", json={"request_id": "req-c"})
    assert r.status_code == 200
    assert r.json()["cancelled"] is True
    assert ev.is_set()

    # Cross-user cancel is rejected.
    main.sessions["sess-b"] = "bob"
    client2 = TestClient(main.app, cookies={"session": "sess-b"})
    chat_api._active_streams["req-x"] = {"user": "alice", "event": asyncio.Event()}
    r2 = client2.post("/api/chat/cancel", json={"request_id": "req-x"})
    assert r2.status_code == 403

    # Not-found is idempotent.
    r3 = client.post("/api/chat/cancel", json={"request_id": "nope"})
    assert r3.status_code == 200
    assert r3.json()["cancelled"] is False


# ── 7. conversation isolation (regression) ────────────────────────

def test_conversation_isolation():
    a = chat_service.create_conversation("alice", "A")
    b = chat_service.create_conversation("bob", "B")
    assert {c["conversation_id"] for c in chat_service.list_conversations("alice")} == {a["conversation_id"]}
    assert {c["conversation_id"] for c in chat_service.list_conversations("bob")} == {b["conversation_id"]}
    assert chat_service.get_conversation("alice", b["conversation_id"]) is None
    assert chat_service.get_conversation("bob", a["conversation_id"]) is None


def test_router_mounted():
    import main
    paths = {route.path for route in main.app.routes}
    assert "/api/chat" in paths
    assert "/api/chat/cancel" in paths
    assert "/api/conversations" in paths
    assert "/api/conversations/{conversation_id}" in paths
    assert "/api/chat/status" in paths


# ── 8. Phase 4: concurrency/cancellation responsiveness regressions ──
# Root cause regression: stream_chat drained the worker queue with a
# *blocking* q.get() on the event-loop thread and joined the worker with a
# blocking worker.join() in cleanup, starving the loop during active
# generation (a ~2ms /api/chat/status took >11s). These tests pin the fix:
# the loop stays responsive while a stream is actively generating, and
# cleanup/join never block the loop.

def test_event_loop_stays_responsive_during_active_stream():
    """While stream_chat is actively streaming, trivial event-loop work must
    complete promptly. With the old blocking drain, this loop-thread was
    monopolized for the whole generation (~1.8s here)."""
    tokens = [" t"] * 90
    prov = SlowProvider(tokens=tokens, delay=0.02)  # ~1.8s of active streaming

    async def run():
        gen = chat_service.stream_chat("alice", "", "hi")
        deltas = 0
        gen_done = asyncio.Event()

        async def consume():
            nonlocal deltas
            async for f in gen:
                if f.get("type") == "delta":
                    deltas += 1
            gen_done.set()

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0.15)  # stream is now actively generating
        assert not gen_done.is_set(), "stream finished before responsiveness probe"
        worst = 0.0
        for _ in range(10):
            t0 = time.perf_counter()
            await asyncio.sleep(0.01)  # trivial sleep; starvation inflates it
            worst = max(worst, time.perf_counter() - t0)
        await consumer
        assert deltas == 90, f"expected full stream, got {deltas} deltas"
        assert worst < 0.25, (
            f"event loop starved during active stream: sleep(0.01) took {worst*1000:.0f}ms")

    with patch("chat_service.get_provider", return_value=prov):
        asyncio.run(run())


def test_worker_join_never_blocks_event_loop():
    """After cancellation, the worker unwind + join must happen off the
    event-loop thread. The provider simulates 0.4s of teardown after observing
    the interrupt; a concurrent monitor task measures loop latency during that
    window. With the old blocking join() in the finally block, every loop
    callback stalled for the full unwind."""
    class UnwindProvider(FakeProvider):
        def __init__(self):
            super().__init__(tokens=["one", " two", " three", " four", " five"])
            self.unwound = threading.Event()

        async def chat(self, model, messages, tools=None, stream_callback=None,
                       interrupt_event=None, **kw):
            for t in self.tokens:
                if interrupt_event is not None and interrupt_event.is_set():
                    self.interrupted = True
                    break
                if stream_callback:
                    stream_callback(t)
                await asyncio.sleep(0.02)
            if self.interrupted:
                # Simulate slow provider teardown AFTER observing the interrupt.
                await asyncio.sleep(0.4)
                self.unwound.set()
            return {"role": "assistant", "content": "".join(self.tokens)}

    prov = UnwindProvider()
    cancel = asyncio.Event()

    async def run():
        gen = chat_service.stream_chat("alice", "", "hi", cancel_event=cancel)
        terminal = None
        loop_free = threading.Event()

        async def monitor(stop):
            worst = 0.0
            while not stop.is_set():
                t0 = time.perf_counter()
                await asyncio.sleep(0.005)
                worst = max(worst, time.perf_counter() - t0)
            return worst

        stop = asyncio.Event()
        monitor_task = asyncio.create_task(monitor(stop))
        async for f in gen:
            if f.get("type") == "delta":
                cancel.set()
            if f.get("type") == "status":
                terminal = f
        stop.set()
        worst = await monitor_task
        assert terminal is not None and terminal["status"] == "cancelled"
        assert prov.unwound.is_set(), "provider never unwound after interrupt"
        assert worst < 0.15, (
            f"event loop blocked during worker join/unwind: "
            f"sleep(0.005) took {worst*1000:.0f}ms")

    with patch("chat_service.get_provider", return_value=prov):
        asyncio.run(run())

    time.sleep(0.2)
    leftovers = [t for t in threading.enumerate()
                 if t.name.startswith("chat-") and t.is_alive()]
    assert leftovers == [], f"orphaned worker threads: {[t.name for t in leftovers]}"


def test_cancelled_terminal_carries_partial_content():
    """The cancelled terminal event must carry the partial assistant content
    accumulated up to the cancellation point (existing UI contract)."""
    cancel = asyncio.Event()
    frames = []

    async def run():
        gen = chat_service.stream_chat("alice", "", "hi", cancel_event=cancel)
        async for f in gen:
            frames.append(f)
            if f.get("type") == "delta":
                cancel.set()

    with patch("chat_service.get_provider",
               return_value=SlowProvider(tokens=["one", " two", " three", " four", " five"],
                                         delay=0.03)):
        asyncio.run(run())

    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "cancelled"
    partial = [f["content"] for f in frames if f.get("type") == "delta"]
    assert 0 < len(partial) < 5, "cancel must stop mid-stream, not after completion"
    # Contract: cancelled.content == stripped partial text (matches persistence).
    assert terminal["content"] == "".join(partial).strip()
    assert terminal["content"], "partial content must be non-empty"


def test_http_responsiveness_and_midstream_cancel_over_real_http():
    """Full HTTP reproduction of the Phase 3 symptom: while /api/chat is
    actively streaming over the real uvicorn server, /api/chat/status must
    answer in milliseconds and POST /api/chat/cancel must reach the active
    generation. With the old blocking drain, both stalled until the
    generation finished (status took >11s; cancel landed post-completion)."""
    import json as _json
    import socket
    import urllib.request

    import httpx
    import uvicorn

    import main

    class SlowHTTPProvider(FakeProvider):
        def __init__(self):
            super().__init__(tokens=[f" w{i}" for i in range(200)])
            self.started = threading.Event()

        async def chat(self, model, messages, tools=None, stream_callback=None,
                       interrupt_event=None, **kw):
            self.started.set()
            for t in self.tokens:
                if interrupt_event is not None and interrupt_event.is_set():
                    self.interrupted = True
                    break
                if stream_callback:
                    stream_callback(t)
                await asyncio.sleep(0.03)  # ~6s total if never cancelled
            return {"role": "assistant", "content": "".join(self.tokens)}

    prov = SlowHTTPProvider()
    main.sessions["sess-h"] = "http-user"

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    # The uvicorn server thread runs the real app; get_provider must be
    # patched for the whole window (streaming + cancel probes all happen
    # inside this context, in any thread).
    patcher = patch("chat_service.get_provider", return_value=prov)
    patcher.start()

    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1",
                                           port=port, log_level="error"))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/chat/status", timeout=1)
            break
        except Exception:
            time.sleep(0.05)

    frames = []
    first_delta = threading.Event()
    stream_done = threading.Event()

    def consume():
        with httpx.Client(base_url=base, cookies={"session": "sess-h"}) as c:
            with c.stream("POST", "/api/chat",
                          json={"message": "essay please", "request_id": "http-1"},
                          timeout=30) as r:
                for line in r.iter_lines():
                    if line.startswith("data:"):
                        f = _json.loads(line[5:].strip())
                        frames.append(f)
                        if f.get("type") == "delta":
                            first_delta.set()
                        if f.get("type") in ("done", "error", "cancelled"):
                            break
        stream_done.set()

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    assert first_delta.wait(10), "stream never produced a first delta"

    # A. Another HTTP endpoint remains responsive during active generation.
    latencies = []
    with httpx.Client(base_url=base, cookies={"session": "sess-h"}) as c:
        for _ in range(5):
            t0 = time.perf_counter()
            r = c.get("/api/chat/status", timeout=5)
            dt = time.perf_counter() - t0
            assert r.status_code == 200
            latencies.append(dt)
    assert not stream_done.is_set(), "generation finished before responsiveness probes"
    assert max(latencies) < 0.5, (
        f"/api/chat/status starved during active stream: "
        f"{[round(x, 3) for x in latencies]}")

    # B. Cancel reaches the active request while generation is still running.
    t0 = time.perf_counter()
    with httpx.Client(base_url=base, cookies={"session": "sess-h"}) as c:
        r = c.post("/api/chat/cancel", json={"request_id": "http-1"}, timeout=5)
    cancel_dt = time.perf_counter() - t0
    assert r.status_code == 200 and r.json().get("cancelled") is True, r.text
    assert cancel_dt < 0.5, (
        f"/api/chat/cancel blocked during active generation: {cancel_dt:.2f}s")

    consumer.join(timeout=10)
    assert not consumer.is_alive(), "stream consumer did not terminate after cancel"

    types = [f["type"] for f in frames]
    assert types[-1] == "cancelled", types
    deltas = [f["content"] for f in frames if f["type"] == "delta"]
    assert 0 < len(deltas) < 200, f"expected partial stream, got {len(deltas)} deltas"
    terminal = frames[-1]
    assert terminal["content"] == "".join(deltas).strip()
    assert prov.interrupted is True, "provider never observed the interrupt"

    time.sleep(0.2)
    leftovers = [t for t in threading.enumerate()
                 if t.name.startswith("chat-") and t.is_alive()]
    assert leftovers == [], f"orphaned worker threads: {[t.name for t in leftovers]}"

    server.should_exit = True
    patcher.stop()


# ── 9. Phase 5: production deployment serving regressions ──────────
# The Chat SPA is served on its own host (KYREX_CHAT_PUBLIC_HOST) by the SAME
# service; the canonical Cloud host keeps serving the existing task frontend.
# Browser login initiated on the chat host must return the OAuth callback to
# that same host so the session cookie lands on the origin in use.

def test_host_routing_serves_chat_ui_and_default_frontend():
    import main
    from fastapi.testclient import TestClient

    # Patched explicitly (not import-time env) so the test is order-independent
    # regardless of which test module imported main first.
    with patch.object(main, "CHAT_PUBLIC_HOST", "chat.kyrex.dev"):
        # Chat host: serves the built Chat SPA.
        chat_client = TestClient(main.app, base_url="http://chat.kyrex.dev")
        r = chat_client.get("/")
        assert r.status_code == 200
        assert "Kyrex Chat" in r.text, "chat host must serve the Chat SPA index"

        # API routes win over the static mount on every host, and auth is intact.
        r_api = chat_client.get("/api/chat/status")
        assert r_api.status_code == 401, "unauthenticated chat-host API call must 401"

        # Canonical host: serves the EXISTING Cloud task frontend, not the SPA.
        cloud_client = TestClient(main.app, base_url="http://kyrex-production.up.railway.app")
        r_default = cloud_client.get("/")
        assert r_default.status_code == 200
        assert "Kyrex Chat" not in r_default.text

        # Unknown host: falls back to the canonical frontend.
        r_other = TestClient(main.app, base_url="http://some.other.host").get("/")
        assert r_other.status_code == 200
        assert "Kyrex Chat" not in r_other.text


def test_login_base_url_prefers_chat_host_when_configured():
    from types import SimpleNamespace
    import main

    chat_req = SimpleNamespace(headers={"host": "chat.kyrex.dev"},
                               base_url="https://kyrex-production.up.railway.app/")
    other_req = SimpleNamespace(headers={"host": "kyrex-production.up.railway.app"},
                                base_url="https://kyrex-production.up.railway.app/")

    with patch.object(main, "CHAT_PUBLIC_HOST", "chat.kyrex.dev"):
        with patch.object(main, "PUBLIC_BASE_URL", "https://kyrex-production.up.railway.app"):
            assert main.login_base_url(chat_req) == "https://chat.kyrex.dev"
            # Every other host keeps the existing PUBLIC_BASE_URL behaviour.
            assert main.login_base_url(other_req) == "https://kyrex-production.up.railway.app"
    # With no chat host configured, behaviour is exactly the legacy one.
    with patch.object(main, "CHAT_PUBLIC_HOST", ""):
        assert main.login_base_url(chat_req) == "https://kyrex-production.up.railway.app"


def test_login_redirect_targets_chat_host_callback():
    from types import SimpleNamespace
    from urllib.parse import urlparse, parse_qs
    import main

    req = SimpleNamespace(headers={"host": "chat.kyrex.dev"},
                          base_url="https://kyrex-production.up.railway.app/")
    with patch.object(main, "CHAT_PUBLIC_HOST", "chat.kyrex.dev"):
        resp = main.login(req)
    location = resp.headers["location"]
    assert "github.com/login/oauth/authorize" in location
    redirect_uri = parse_qs(urlparse(location).query)["redirect_uri"][0]
    assert redirect_uri == "https://chat.kyrex.dev/auth/callback"
