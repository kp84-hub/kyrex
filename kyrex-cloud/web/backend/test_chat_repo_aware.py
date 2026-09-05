"""Tests for repo-aware Kyrex Chat (attached workspace mode).

Proves — without weakening any security check and without any live LLM —
that a Kyrex Chat conversation with an attached workspace:

  1. is served by the REAL Kyrex engine/tool path
     (``kyrex.core.PlaneExecute`` dispatch → ``kyrex.toolBox`` tools), and
     can actually read files from a controlled test workspace;
  2. receives the workspace context (working directory + local file tree)
     in the engine's bootstrap system prompt;
  3. can use the read-only inspection tools (read_local_file / search);
  4. CANNOT write, edit, delete, or run commands:
       * the tool allowlist (KYREX_ALLOWED_TOOLS) hides write/command tools
         from the model AND blocks them in the dispatch loop,
       * KYREX_READ_ONLY_REPO=1 makes the toolbox refuse writes/edits,
       * the chat backend answers every propose_edit / confirm_request
         with an explicit denial;
  5. the engine bridge process is spawned with the workspace as its working
     directory (the VS Code / Tauri IDE / headless-agent convention);
  6. pure-conversation behavior is unchanged when no workspace is attached.

No test in this file performs a real LLM call:
  * Tier 1 drives the REAL engine core with a scripted provider;
  * Tier 2 spawns the REAL ``core_bridge.py`` (handshake + provider-error
    mapping only — the provider points at a closed local port, so the engine
    fails fast without any network egress);
  * Tier 3 exercises the full HTTP/SSE surface against a scripted NDJSON
    bridge that speaks the engine protocol.

Run: pytest test_chat_repo_aware.py
"""

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-chat-tests")
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
ALLOWED = chat_service.CHAT_ENGINE_ALLOWED_TOOLS


# ── helpers ────────────────────────────────────────────────────────

def _make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "testws"
    ws.mkdir()
    (ws / "PROOF.txt").write_text(PROOF)
    (ws / "NOTES.txt").write_text(f"note:{PROOF}\nsecond line\n")
    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text("VALUE = 41\n")
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


async def _frames(agen):
    out = []
    async for f in agen:
        out.append(f)
    return out


def _terminal(frames):
    status = [f for f in frames if f.get("type") == "status"]
    return status[-1] if status else None


def _deltas(frames):
    return [f["content"] for f in frames if f.get("type") == "delta"]


def _seed_session(user: str, token: str):
    import main
    main.sessions[token] = user


# ── workspace registry (server-controlled; fail-closed) ───────────

def test_workspace_registry_parses_dict_and_list(tmp_path, monkeypatch):
    ws = _make_workspace(tmp_path)
    monkeypatch.delenv("KYREX_CHAT_WORKSPACE", raising=False)
    monkeypatch.setenv(
        "KYREX_CHAT_WORKSPACES",
        json.dumps({"testws": str(ws)}))
    assert chat_service.resolve_workspace("testws") == ws.resolve()
    assert chat_service.resolve_workspace("nope") is None
    monkeypatch.setenv(
        "KYREX_CHAT_WORKSPACES",
        json.dumps([{"id": "w2", "path": str(ws), "name": "My Repo"}]))
    assert chat_service.resolve_workspace("w2") == ws.resolve()
    names = {w["id"]: w["name"] for w in chat_service.list_workspaces()}
    assert names == {"w2": "My Repo"}


def test_workspace_registry_rejects_relative_and_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("KYREX_CHAT_WORKSPACE", raising=False)
    monkeypatch.setenv(
        "KYREX_CHAT_WORKSPACES",
        json.dumps({"rel": "some/relative/path",
                    "missing": str(tmp_path / "does-not-exist")}))
    assert chat_service.resolve_workspace("rel") is None
    assert chat_service.resolve_workspace("missing") is None
    avail = {w["id"]: w["available"] for w in chat_service.list_workspaces()}
    assert avail == {"rel": False, "missing": False}


def test_workspace_registry_single_env_var(monkeypatch, tmp_path):
    monkeypatch.delenv("KYREX_CHAT_WORKSPACES", raising=False)
    monkeypatch.setenv("KYREX_CHAT_WORKSPACE", str(_make_workspace(tmp_path)))
    assert chat_service.resolve_workspace("default") is not None
    assert chat_service.resolve_workspace("default").is_dir()


def test_list_workspaces_never_exposes_paths(monkeypatch, tmp_path):
    monkeypatch.delenv("KYREX_CHAT_WORKSPACE", raising=False)
    monkeypatch.setenv(
        "KYREX_CHAT_WORKSPACES", json.dumps({"testws": str(_make_workspace(tmp_path))}))
    for w in chat_service.list_workspaces():
        assert set(w.keys()) == {"id", "name", "available"}
        assert "path" not in w


# ── Tier 1: the REAL engine core + toolbox dispatch (no LLM) ──────

class ScriptedProvider:
    """Duck-typed provider returning scripted rounds (tool_calls / content)."""

    def __init__(self, steps):
        self.steps = list(steps)

    async def chat(self, model, messages, tools=None, stream_callback=None, **kw):
        step = self.steps.pop(0)
        if stream_callback and step.get("content"):
            stream_callback(step["content"])
        return {
            "role": "assistant",
            "content": step.get("content"),
            "tool_calls": step.get("tool_calls"),
        }


class _StubMCP:
    def __init__(self):
        self.servers = {}

    def start_all(self):
        pass

    def get_tool_schemas(self):
        return []

    def call_tool(self, *a, **k):
        raise RuntimeError("mcp disabled in test")


def _build_engine(tmp_path, monkeypatch):
    """Real PlaneExecute in the test workspace (cwd = workspace, exactly like
    a spawned bridge), with only the provider and MCP manager scripted."""
    ws = _make_workspace(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))          # hermetic: no user .px/.kyrex
    # The engine workspace is its cwd — an inherited WORKSPACE_ROOT /
    # PROJECT_SOURCE_ROOT (e.g. running inside an agent sandbox) would make
    # the toolbox validate paths against a foreign root.
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("PROJECT_SOURCE_ROOT", raising=False)
    monkeypatch.chdir(ws)                          # engine workspace = cwd
    monkeypatch.setattr("kyrex.core._WORKSPACE_ROOT", str(ws))
    monkeypatch.setattr("kyrex.core.MCPManager", _StubMCP)
    monkeypatch.setenv("KYREX_READ_ONLY_REPO", "1")
    monkeypatch.setenv("KYREX_ALLOWED_TOOLS", ALLOWED)

    from kyrex.core import PlaneExecute
    engine = PlaneExecute(provider="openai", api_key="sk-test", model="test-model")
    return ws, engine


def _tool_call(name, args_json, call_id="c1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": args_json}}


def test_real_engine_reads_file_through_real_tool_path(tmp_path, monkeypatch):
    """ACCEPTANCE: Kyrex Chat's engine path reads a REAL file from the
    attached workspace through the REAL dispatch/toolbox tool path."""
    ws, engine = _build_engine(tmp_path, monkeypatch)
    engine.provider = ScriptedProvider([
        # Round 1: the model asks to read the file (a registered tool).
        {"tool_calls": [_tool_call("read_local_file", '{"path": "NOTES.txt"}', "c1")]},
        # Round 2: the model answers from the tool result and completes.
        {"content": f"The note file contains {PROOF}.",
         "tool_calls": [_tool_call("task_complete", '{"summary": "read done"}', "c2")]},
    ])

    result, _ = asyncio.run(engine.chat("What does NOTES.txt say?"))

    assert PROOF in result, f"engine tool path did not return the file content: {result!r}"
    tool_msgs = [m for m in engine.session.history if m.get("role") == "tool"]
    assert tool_msgs, "no tool message recorded — the tool path did not run"
    assert any(PROOF in str(m.get("content")) for m in tool_msgs), (
        f"tool result did not carry the real file content: {[m.get('content') for m in tool_msgs]}")
    # The read tool resolved the file INSIDE the workspace.
    assert any(f"NOTES.txt" in str(m.get("content")) for m in tool_msgs)


def test_real_engine_receives_workspace_context(tmp_path, monkeypatch):
    ws, engine = _build_engine(tmp_path, monkeypatch)
    engine.provider = ScriptedProvider([
        {"content": "ok.", "tool_calls": [_tool_call("task_complete", '{"summary": "s"}', "c1")]},
    ])
    asyncio.run(engine.chat("hello"))

    system = engine.session.history[0]
    assert system["role"] == "system"
    content = system["content"]
    assert "## Working Directory:" in content
    assert str(ws) in content, "bootstrap system prompt must carry the workspace path"
    assert "## Local File Tree:" in content
    assert "PROOF.txt" in content and "NOTES.txt" in content, (
        "bootstrap file tree must list the workspace files")


def test_real_engine_blocks_writes_and_commands(tmp_path, monkeypatch):
    """Read-only enforcement, layer 1 (dispatch allowlist): write/command
    tools are never EXECUTED — even when the model emits tool_calls for them
    — and nothing is written to the workspace."""
    ws, engine = _build_engine(tmp_path, monkeypatch)
    engine.provider = ScriptedProvider([
        {"tool_calls": [_tool_call("write_file_with_gate",
                                   '{"path": "evil.txt", "content": "nope"}', "c1")]},
        {"tool_calls": [_tool_call("run_command", '{"command": "touch pwned.txt"}', "c2")]},
        {"tool_calls": [_tool_call("delete-via-search",
                                   '{"pattern": "x"}', "c3")]},
        {"content": "Blocked as expected.",
         "tool_calls": [_tool_call("task_complete", '{"summary": "done"}', "c4")]},
    ])

    result, _ = asyncio.run(engine.chat("try to modify the repo"))

    tool_msgs = [str(m.get("content")) for m in engine.session.history
                 if m.get("role") == "tool"]
    joined = "\n".join(tool_msgs)
    # Every non-inspection tool is refused by the dispatch guard.
    assert joined.count("not permitted in this session") == 3, joined
    # Nothing was written into the workspace.
    top_level = {p.name for p in ws.iterdir()}
    assert "evil.txt" not in top_level
    assert "pwned.txt" not in top_level


def test_toolbox_read_only_layer_blocks_writes_without_allowlist(tmp_path, monkeypatch):
    """Read-only enforcement, layer 2 (KYREX_READ_ONLY_REPO): with the
    allowlist OFF (default engine env), the REAL toolbox still refuses file
    writes and edits outright."""
    ws, engine = _build_engine(tmp_path, monkeypatch)
    monkeypatch.delenv("KYREX_ALLOWED_TOOLS", raising=False)
    engine.provider = ScriptedProvider([
        {"tool_calls": [_tool_call("write_file_with_gate",
                                   '{"path": "evil.txt", "content": "nope"}', "c1")]},
        {"tool_calls": [_tool_call("edit_file",
                                   '{"path": "PROOF.txt", "search_text": "X", "replace_text": "Y"}',
                                   "c2")]},
        {"content": "Blocked as expected.",
         "tool_calls": [_tool_call("task_complete", '{"summary": "done"}', "c3")]},
    ])

    asyncio.run(engine.chat("try to modify the repo"))

    tool_msgs = [str(m.get("content")) for m in engine.session.history
                 if m.get("role") == "tool"]
    joined = "\n".join(tool_msgs)
    assert "Read-only repository: file writes are disabled." in joined, joined
    top_level = {p.name for p in ws.iterdir()}
    assert "evil.txt" not in top_level
    assert "PROOF.txt" in top_level and "unmodified" or True
    assert "KYREX_CHAT_PROOF_MARKER_12345" in (ws / "PROOF.txt").read_text()


def test_allowlist_filters_advertised_tools_and_default_unaffected(tmp_path, monkeypatch):
    """The allowlist must (a) hide non-listed tools from the schema and
    (b) leave the default (env unset) behavior byte-for-byte unchanged."""
    ws, engine = _build_engine(tmp_path, monkeypatch)  # sets KYREX_ALLOWED_TOOLS
    allowed_set = {s.strip() for s in ALLOWED.split(",")}
    names = {s["function"]["name"] for s in engine._get_all_tools_schema()}
    assert names <= allowed_set, names
    assert "read_local_file" in names and "search" in names
    assert "run_command" not in names
    assert "write_file_with_gate" not in names
    assert "edit_file" not in names

    # Default: env unset → full builtin set (existing surfaces unaffected).
    monkeypatch.delenv("KYREX_ALLOWED_TOOLS", raising=False)
    names_default = {s["function"]["name"] for s in engine._get_all_tools_schema()}
    assert "run_command" in names_default
    assert "write_file_with_gate" in names_default
    assert "read_local_file" in names_default


# ── Tier 2: the REAL core_bridge.py subprocess ─────────────────────

def test_real_bridge_handshake_in_workspace(tmp_path, monkeypatch):
    """The chat backend spawns the REAL engine bridge with the workspace as
    its working directory (session_state.context proves the cwd)."""
    ws = _make_workspace(tmp_path)
    # Deterministic provider env for the child (the embedding handshake
    # KYREX_VSCODE=1 bypasses the config-file startup gate; env keys override
    # any config values). HOME is NOT patched: relocating HOME would relocate
    # the interpreter's user site-packages and strip the child of openai.
    monkeypatch.setenv("KYREX_PROVIDER", "openai")
    monkeypatch.setenv("KYREX_MODEL", "gpt-test")
    monkeypatch.setenv("KYREX_API_KEY", "sk-test")
    monkeypatch.setenv("KYREX_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:1/v1")

    sess = chat_service.EngineSession(ws, chat_service._resolve_provider())
    try:
        assert sess._proc.poll() is None
        assert sess.session_state is not None, "bridge must emit session_state at startup"
        ctx = sess.session_state.get("context")
        assert ctx is not None
        assert Path(ctx).resolve() == ws.resolve(), (
            f"engine cwd mismatch: {ctx} != {ws}")
    finally:
        sess.close()
    deadline = time.time() + 10
    while sess._proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    assert sess._proc.poll() is not None, "engine process must terminate on close()"


def test_real_bridge_provider_error_maps_to_error(tmp_path, monkeypatch):
    """Full real-bridge turn against a closed local port: the engine returns
    the provider's swallowed-error content and the chat client must map it to
    an error outcome (never persist it as a successful reply)."""
    ws = _make_workspace(tmp_path)
    monkeypatch.setenv("KYREX_PROVIDER", "openai")
    monkeypatch.setenv("KYREX_MODEL", "gpt-test")
    monkeypatch.setenv("KYREX_API_KEY", "sk-test")
    # Closed port → instant connection refusal; no external network egress.
    monkeypatch.setenv("KYREX_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:1/v1")

    sess = chat_service.EngineSession(ws, chat_service._resolve_provider())
    try:
        tokens = []
        final, err = sess.run_turn("hello", tokens.append)
        assert err is not None, f"expected a provider error, got final={final!r}"
        assert "error" in err.lower()
        # The turn completed through the bridge protocol (chat_done + IDLE).
        assert sess._proc.poll() is None
    finally:
        sess.close()


# ── Tier 3: full HTTP/SSE surface with a scripted NDJSON bridge ────

# A scripted engine bridge that speaks the REAL protocol: session_state,
# phase IDLE, token frames, chat_done, propose_edit / confirm_request
# (awaiting the client's decision), and interrupt handling. Its default
# turn reads PROOF.txt from ITS OWN CWD — proving end-to-end that the chat
# backend spawned the engine in the attached workspace.
FAKE_BRIDGE = r'''
import json, os, sys, threading, queue, time

def out(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()

state = {"interrupt": False}
decisions = {}
events = {}
q = queue.Queue()

def reader():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            p = json.loads(line)
        except Exception:
            continue
        t = p.get("type")
        if t == "interrupt":
            state["interrupt"] = True
        elif t == "edit_decision":
            decisions[p.get("editId")] = bool(p.get("accepted"))
            ev = events.get(p.get("editId"))
            if ev:
                ev.set()
        elif t == "confirm_response":
            decisions[p.get("id")] = bool(p.get("approved"))
            ev = events.get(p.get("id"))
            if ev:
                ev.set()
        elif t == "chat":
            q.put(p)

threading.Thread(target=reader, daemon=True).start()

out({"type": "session_state", "model": "fake", "provider": "fake",
     "context": os.getcwd(), "files": {}})
out({"type": "phase", "value": "IDLE"})

while True:
    p = q.get()
    text = p.get("content", "")
    if "try-write" in text:
        ev = threading.Event()
        events["e1"] = ev
        out({"type": "propose_edit", "editId": "e1",
             "filePath": os.path.join(os.getcwd(), "evil.txt"),
             "content": "hacked"})
        if "e1" in decisions:
            ev.set()
        ev.wait(10)
        reply = f"[edit_accepted={decisions.get('e1')}]"
    elif "confirm-del" in text:
        ev = threading.Event()
        events["c1"] = ev
        out({"type": "confirm_request", "id": "c1", "value": "deletion",
             "path": os.path.join(os.getcwd(), "NOTES.txt"), "diff": "DELETE?"})
        if "c1" in decisions:
            ev.set()
        ev.wait(10)
        reply = f"[confirm_approved={decisions.get('c1')}]"
    elif text.startswith("long"):
        parts = []
        for i in range(30):
            if state["interrupt"]:
                break
            w = f" w{i}"
            parts.append(w)
            out({"type": "token", "content": w})
            time.sleep(0.05)
        out({"type": "chat_done",
             "content": "" if state["interrupt"] else "".join(parts),
             "reasoning": ""})
        out({"type": "phase", "value": "IDLE"})
        state["interrupt"] = False
        continue
    else:
        try:
            content = open("PROOF.txt").read()
        except Exception as e:
            content = f"ERR {e}"
        reply = "PROOF:" + content
    out({"type": "token", "content": reply})
    out({"type": "chat_done", "content": reply, "reasoning": ""})
    out({"type": "phase", "value": "IDLE"})
'''


@pytest.fixture()
def fake_engine(tmp_path, monkeypatch):
    """Write the scripted bridge, register the workspace, point the chat
    service's bridge path at it, and provide an HTTP test client."""
    bridge = tmp_path / "fake_bridge.py"
    bridge.write_text(FAKE_BRIDGE)
    ws = _make_workspace(tmp_path)
    monkeypatch.delenv("KYREX_CHAT_WORKSPACE", raising=False)
    monkeypatch.setenv(
        "KYREX_CHAT_WORKSPACES", json.dumps({"testws": str(ws)}))
    monkeypatch.setattr(chat_service, "ENGINE_BRIDGE_PATH", str(bridge))

    import main
    from fastapi.testclient import TestClient
    _seed_session("wsuser", "sess-ws")
    client = TestClient(main.app, cookies={"session": "sess-ws"})
    return client, ws


def _sse_frames(response):
    frames = []
    for line in response.iter_lines():
        if line.startswith("data:"):
            frames.append(json.loads(line[5:].strip()))
    return frames


def _new_conversation(client):
    r = client.post("/api/conversations", json={})
    assert r.status_code == 200
    return r.json()["conversation_id"]


def test_workspace_attach_validation_and_persistence(fake_engine):
    client, ws = fake_engine
    conv_id = _new_conversation(client)

    # Unknown id → 400 (and never a path).
    r = client.post("/api/chat/workspace",
                    json={"conversation_id": conv_id, "workspace_id": "../../etc"})
    assert r.status_code == 400

    # Unknown conversation → 404.
    r = client.post("/api/chat/workspace",
                    json={"conversation_id": "nope", "workspace_id": "testws"})
    assert r.status_code == 404

    # Attach → persisted on the conversation and visible in list + get.
    r = client.post("/api/chat/workspace",
                    json={"conversation_id": conv_id, "workspace_id": "testws"})
    assert r.status_code == 200
    body = r.json()
    assert body["workspace_id"] == "testws"
    assert body["workspace_name"] == "testws"
    got = client.get(f"/api/conversations/{conv_id}").json()
    assert got["workspace_id"] == "testws"
    listed = client.get("/api/conversations").json()["conversations"]
    mine = next(c for c in listed if c["conversation_id"] == conv_id)
    assert mine["workspace_id"] == "testws"

    # Detach (null) → pure conversation again.
    r = client.post("/api/chat/workspace",
                    json={"conversation_id": conv_id, "workspace_id": None})
    assert r.status_code == 200 and r.json()["workspace_id"] is None
    got = client.get(f"/api/conversations/{conv_id}").json()
    assert got.get("workspace_id") is None


def test_repo_aware_turn_reads_workspace_file_over_http(fake_engine):
    """END-TO-END: POST /api/chat on a workspace-attached conversation spawns
    the engine bridge in the workspace; the turn's output contains content
    read from PROOF.txt in that workspace, and the reply is persisted."""
    client, ws = fake_engine
    conv_id = _new_conversation(client)
    r = client.post("/api/chat/workspace",
                    json={"conversation_id": conv_id, "workspace_id": "testws"})
    assert r.status_code == 200

    # No workspace_id in the body → the stored binding is used.
    with client.stream("POST", "/api/chat",
                       json={"conversation_id": conv_id,
                             "message": "read the proof file",
                             "request_id": "r-proof"}) as resp:
        assert resp.status_code == 200
        frames = _sse_frames(resp)

    types = [f["type"] for f in frames]
    assert types[0] == "conversation"
    assert types[-1] == "done"
    full = "".join(f["content"] for f in frames if f["type"] == "delta")
    assert PROOF in full, f"workspace file content missing from reply: {full!r}"
    assert full.startswith("PROOF:")

    conv = client.get(f"/api/conversations/{conv_id}").json()
    assistants = [m for m in conv["messages"] if m["role"] == "assistant"]
    assert len(assistants) == 1
    assert PROOF in assistants[0]["content"]


def test_repo_aware_denies_edit_proposal_and_confirm(fake_engine):
    """propose_edit / confirm_request emitted by the engine must be answered
    with an explicit DENIAL by the chat backend — never auto-approved."""
    client, ws = fake_engine
    conv_id = _new_conversation(client)
    client.post("/api/chat/workspace",
                json={"conversation_id": conv_id, "workspace_id": "testws"})

    with client.stream("POST", "/api/chat",
                       json={"conversation_id": conv_id,
                             "message": "try-write something",
                             "request_id": "r-edit"}) as resp:
        frames = _sse_frames(resp)
    assert frames[-1]["type"] == "done"
    full = "".join(f["content"] for f in frames if f["type"] == "delta")
    assert "[edit_accepted=False]" in full, full
    # The workspace file was never touched (fake bridge only proposes).
    assert not (ws / "evil.txt").exists()

    with client.stream("POST", "/api/chat",
                       json={"conversation_id": conv_id,
                             "message": "confirm-del NOTES.txt",
                             "request_id": "r-del"}) as resp:
        frames = _sse_frames(resp)
    full = "".join(f["content"] for f in frames if f["type"] == "delta")
    assert "[confirm_approved=False]" in full, full
    assert (ws / "NOTES.txt").exists()

    sess = chat_service._engine_sessions[("wsuser", conv_id)]
    kinds = [d["kind"] for d in sess.denied_requests]
    assert "edit" in kinds and "deletion" in kinds


def test_repo_aware_cancel_mid_turn_not_persisted(fake_engine):
    """Cancellation of a repo-aware turn over REAL HTTP: partial text
    preserved, terminal cancelled frame, and NO assistant message persisted
    (persistence parity with the pure-chat path). Uses a real uvicorn server
    because starlette's TestClient buffers SSE responses, which makes
    mid-stream cancel POSTs land after the stream has finished."""
    import socket
    import httpx
    import uvicorn

    import main

    client, ws = fake_engine
    conv_id = _new_conversation(client)
    client.post("/api/chat/workspace",
                json={"conversation_id": conv_id, "workspace_id": "testws"})

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1",
                                           port=port, log_level="error"))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            import urllib.request
            urllib.request.urlopen(base + "/api/chat/status", timeout=1)
            break
        except Exception:
            time.sleep(0.05)

    frames = []
    first_delta = threading.Event()

    def consume():
        with httpx.Client(base_url=base, cookies={"session": "sess-ws"}) as c:
            with c.stream("POST", "/api/chat",
                          json={"conversation_id": conv_id,
                                "message": "long essay please",
                                "request_id": "r-cancel"},
                          timeout=30) as resp:
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    f = json.loads(line[5:].strip())
                    frames.append(f)
                    if f["type"] == "delta":
                        first_delta.set()
                    if f["type"] in ("done", "error", "cancelled"):
                        break

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    assert first_delta.wait(10), "stream never produced a first delta"

    with httpx.Client(base_url=base, cookies={"session": "sess-ws"}) as c:
        rc = c.post("/api/chat/cancel", json={"request_id": "r-cancel"}, timeout=5)
    assert rc.status_code == 200 and rc.json()["cancelled"] is True, rc.text

    consumer.join(timeout=15)
    assert not consumer.is_alive(), "stream consumer did not terminate after cancel"

    terminal = frames[-1]
    assert terminal["type"] == "cancelled", frames
    partial = "".join(f["content"] for f in frames if f["type"] == "delta")
    assert 0 < len(partial) < 30, f"expected a mid-turn partial, got {len(partial)} chars"
    assert terminal["content"] == partial.strip()

    conv = client.get(f"/api/conversations/{conv_id}").json()
    assistants = [m for m in conv["messages"] if m["role"] == "assistant"]
    assert assistants == [], "cancelled turn must not persist an assistant message"

    server.should_exit = True


def test_pure_chat_unaffected_when_registry_present(fake_engine):
    """With a workspace registry configured, a conversation with NO attached
    workspace still takes the pure-conversation provider path."""
    client, ws = fake_engine

    class FakeProvider:
        async def chat(self, model, messages, tools=None, stream_callback=None, **kw):
            for t in ["Hi", " there"]:
                if stream_callback:
                    stream_callback(t)
            return {"role": "assistant", "content": "Hi there"}

    with patch("chat_service.get_provider", return_value=FakeProvider()):
        with client.stream("POST", "/api/chat",
                           json={"message": "hi", "request_id": "r-pure"}) as resp:
            frames = _sse_frames(resp)

    assert frames[-1]["type"] == "done"
    full = "".join(f["content"] for f in frames if f["type"] == "delta")
    assert full == "Hi there"
    # No engine session was created for a pure-chat conversation.
    assert len(chat_service._engine_sessions) == 0
    convs = client.get("/api/conversations").json()["conversations"]
    assert all(c.get("workspace_id") is None for c in convs)


def test_unknown_workspace_id_rejected_before_stream(fake_engine):
    client, ws = fake_engine
    r = client.post("/api/chat",
                    json={"message": "hi", "workspace_id": "not-registered"})
    assert r.status_code == 400
    assert "not-registered" in r.json()["detail"]


def test_status_reports_provider_and_workspace_count(fake_engine):
    client, ws = fake_engine
    r = client.get("/api/chat/status")
    assert r.status_code == 200
    body = r.json()
    # "available" is the PROVIDER state — the UI labels it "Provider ready".
    assert body["available"] is True
    assert body["provider"] == "openai/gpt-test"
    assert body["workspaces"] == 1
