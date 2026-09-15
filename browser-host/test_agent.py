"""Tests for the host-side Browser Host agent (``browser-host/agent.py``).

Drive the agent against a SCRIPTED Cloud (a fake connection whose frames are
pre-seeded) so the agent's handshake, host-side allowlist enforcement, task
execution, the approval pause/resume handshake, reconnect/restart recovery, and
CDP redaction are all deterministic and need no network.

Run: python3 -m pytest test_agent.py
"""
import json
import os
import queue
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent as host_agent          # noqa: E402
import host_allowlist as ha         # noqa: E402


# ── fakes ─────────────────────────────────────────────────────────────

def _frame(type_, payload):
    return {"v": 1, "type": type_, "id": None, "ts": 0.0, "payload": payload}


class ScriptedConn:
    """A Cloud whose frames are pre-seeded; raises when the script runs dry."""

    def __init__(self, frames, *, raise_when_empty=True):
        self._in = queue.Queue()
        for f in frames:
            self._in.put(f)
        self.sent = []
        self.closed = False
        self._raise = raise_when_empty

    def send(self, f):
        self.sent.append(f)

    def recv(self, timeout=None):
        # Cap the wait so an exhausted script fails fast instead of waiting out
        # the agent's (production-sized) heartbeat interval.
        wait = 0.05 if timeout is None else min(float(timeout), 0.05)
        try:
            return self._in.get(timeout=wait)
        except queue.Empty:
            if self._raise:
                raise host_agent.AgentError("connection closed")
            return None

    def close(self):
        self.closed = True

    def types(self):
        return [f.get("type") for f in self.sent]


class FakeExecutor:
    def __init__(self, frames):
        self._frames = list(frames)
        self.decisions = []
        self.started = False

    def start(self):
        self.started = True

    def read(self):
        return self._frames.pop(0) if self._frames else None

    def send(self, decision):
        self.decisions.append(decision)

    def stop(self):
        return None


HELLO_OK = _frame("hello_ok", {"session_id": "sess-1", "protocol": 1,
                               "heartbeat_interval": 15})
HELLO_ERR = _frame("hello_err", {"reason": "authentication failed"})


_PROFILES = tempfile.mkdtemp(prefix="kx-agent-profiles-")


def _config(**kw):
    base = dict(host_id="host-1", owner="owner-1", secret="enroll-secret",
                cloud_url="wss://cloud.test", allowlist=["example.com"],
                profiles_root=_PROFILES)
    base.update(kw)
    return host_agent.HostConfig(**base)


def _task(task_text, *, task_id="t1", owner="owner-1", bot="bot-1",
          allowlist=("example.com",)):
    return _frame("task", {"task_id": task_id, "owner": owner, "bot_id": bot,
                           "task_text": task_text, "allowlist": list(allowlist)})


CAND = '{"actions": [{"action": "navigate", "url": "https://example.com/"}]}'
EVIL = '{"actions": [{"action": "navigate", "url": "https://evil.com/"}]}'


# ── 1. host_allowlist ─────────────────────────────────────────────────

def test_normalize_and_domain_matching():
    assert ha.normalize_host("https://Example.com/x") == "example.com"
    assert ha.normalize_host("example.com:443") == "example.com"
    assert ha.domain_allowed("https://a.example.com/x", ["example.com"])[0] is True
    # Boundary is a dot: example.com.evil.com must not match example.com.
    assert ha.domain_allowed("https://example.com.evil.com/", ["example.com"])[0] is False


def test_effective_allowlist_intersects_and_fails_closed():
    assert ha.effective_allowlist(["example.com"], ["example.com", "evil.com"]) == \
        ["example.com"]
    # The host adopts the Cloud list when it has none of its own.
    assert ha.effective_allowlist([], ["example.com"]) == ["example.com"]
    # Neither list -> deny all.
    assert ha.effective_allowlist([], []) == []


def test_preflight_blocks_off_list_and_empty_allowlist():
    assert ha.preflight(CAND, ["example.com"])[0] is True
    ok, reason = ha.preflight(EVIL, ["example.com"])
    assert ok is False and "evil.com" in reason
    assert ha.preflight(CAND, [])[0] is False
    assert ha.preflight("not json", ["example.com"])[0] is False


def test_redaction_scrubs_cdp_and_secrets():
    text = "ws://127.0.0.1:9222/devtools/browser/x secret=hunter2"
    out = ha.redact_text(text)
    assert "devtools" not in out and "hunter2" not in out
    assert ha.contains_cdp_url("see http://127.0.0.1:9222/json/version") is True
    assert ha.contains_cdp_url("just a normal sentence") is False


# ── 2. handshake ──────────────────────────────────────────────────────

def test_handshake_succeeds_and_sends_a_proof():
    conn = ScriptedConn([HELLO_OK])
    agent = host_agent.HostAgent(_config(), connect=lambda: conn)
    # Drive only the handshake.
    agent._handshake(conn)
    hello = conn.sent[0]
    assert hello["type"] == "hello"
    assert hello["payload"]["host_id"] == "host-1"
    assert hello["payload"]["proof"] and "secret" not in hello["payload"]
    assert agent._authed is True


def test_handshake_rejection_raises_auth_error():
    conn = ScriptedConn([HELLO_ERR])
    agent = host_agent.HostAgent(_config(), connect=lambda: conn)
    with pytest.raises(host_agent.AuthError):
        agent._handshake(conn)


def test_proof_matches_the_cloud_formula():
    import hashlib
    import hmac
    cfg = _config()
    expected = hmac.new(b"enroll-secret", b"host-1.n1", hashlib.sha256).hexdigest()
    assert cfg.proof("n1") == expected


# ── 3. task execution ─────────────────────────────────────────────────

def test_task_round_trip_forwards_verdict():
    verdict = _frame("verdict", {"task_id": "t1", "op_id": "o1",
                                 "decision": "ALLOW"})
    conn = ScriptedConn([HELLO_OK, _task(CAND), verdict])
    created = []

    def factory(task_text, env):
        ex = FakeExecutor([
            {"kind": "operation", "op": "browser.navigate",
             "target": "https://example.com/", "summary": "navigate"},
            {"kind": "result", "result": {"status": "no_changes"}},
        ])
        created.append(ex)
        return ex

    agent = host_agent.HostAgent(_config(), connect=lambda: conn,
                                 executor_factory=factory)
    with pytest.raises(host_agent.AgentError):     # script runs dry -> raises
        agent.run_once()
    assert "operation" in conn.types()
    assert "result" in conn.types()
    assert created[0].decisions == ["ALLOW"]


def test_task_approval_pause_resume():
    verdict = _frame("verdict", {"task_id": "t1", "op_id": "o1",
                                 "decision": "APPROVE"})
    decision = _frame("approval_decision", {"task_id": "t1",
                                            "approval_id": "a1",
                                            "decision": "APPROVED"})
    conn = ScriptedConn([HELLO_OK, _task(CAND), verdict, decision])
    created = []

    def factory(task_text, env):
        ex = FakeExecutor([
            {"kind": "operation", "op": "browser.submit",
             "target": "https://example.com/x", "summary": "submit"},
            {"kind": "approval", "tier": 2, "summary": "submit",
             "detail": "consequential", "token": "SUBMIT ab12"},
            {"kind": "result", "result": {"status": "ok"}},
        ])
        created.append(ex)
        return ex

    agent = host_agent.HostAgent(_config(), connect=lambda: conn,
                                 executor_factory=factory)
    with pytest.raises(host_agent.AgentError):
        agent.run_once()
    assert created[0].decisions == ["APPROVE", "APPROVED"]
    assert "approval" in conn.types()


def test_host_blocks_off_allowlist_task_without_running_it():
    conn = ScriptedConn([HELLO_OK, _task(EVIL)])
    created = []
    agent = host_agent.HostAgent(
        _config(), connect=lambda: conn,
        executor_factory=lambda t, e: created.append(1) or FakeExecutor([]))
    with pytest.raises(host_agent.AgentError):
        agent.run_once()
    results = [f for f in conn.sent if f["type"] == "result"]
    assert results and "blocked on host" in json.dumps(results[0])
    assert created == []                # the operator was never started


def test_task_refused_when_not_authenticated():
    # A task frame off an unauthenticated channel must be ignored.
    agent = host_agent.HostAgent(_config(), connect=lambda: ScriptedConn([]))
    conn = ScriptedConn([])
    agent._authed = False
    agent._handle_task(conn, {"task_id": "t", "owner": "o", "bot_id": "b",
                              "task_text": CAND})
    assert conn.sent == []


def test_managed_env_sets_isolation_keys():
    verdict = _frame("verdict", {"task_id": "t1", "op_id": "o1",
                                 "decision": "ALLOW"})
    conn = ScriptedConn([HELLO_OK, _task(CAND), verdict])
    seen = {}

    def factory(task_text, env):
        seen.update(env)
        return FakeExecutor([
            {"kind": "operation", "op": "browser.navigate",
             "target": "https://example.com/", "summary": "n"},
            {"kind": "result", "result": {"status": "no_changes"}},
        ])

    agent = host_agent.HostAgent(_config(), connect=lambda: conn,
                                 executor_factory=factory)
    with pytest.raises(host_agent.AgentError):
        agent.run_once()
    assert seen["KYREX_BOT_ID"] == "bot-1"
    assert seen["KYREX_BOT_OWNER"] == "owner-1"
    assert seen["KYREX_BROWSER_MANAGED"] == "1"
    assert seen["KYREX_BROWSER_SESSION_DIR"].endswith(
        os.path.join("bot-bot-1", "owner-owner-1"))


# ── 4. reconnect / restart recovery ───────────────────────────────────

def test_run_forever_reconnects_after_a_drop(monkeypatch):
    state = {"n": 0, "stopped": False}

    def connect():
        state["n"] += 1
        if state["n"] == 1:
            raise host_agent.AgentError("cloud unreachable")
        return ScriptedConn([HELLO_OK])     # authenticates, then runs dry

    def sleep(_seconds):
        # Stop only after the SECOND connect, so the first backoff still runs
        # and the reconnect is actually attempted.
        if state["n"] >= 2:
            state["stopped"] = True

    agent = host_agent.HostAgent(_config(), connect=connect, sleep=sleep,
                                 stop=lambda: state["stopped"])
    agent.run_forever()
    assert state["n"] == 2, "the agent must reconnect after a drop"


# ── 5. no public CDP ──────────────────────────────────────────────────

def test_agent_emits_no_cdp_url_or_listener():
    verdict = _frame("verdict", {"task_id": "t1", "op_id": "o1",
                                 "decision": "ALLOW"})
    conn = ScriptedConn([HELLO_OK, _task(CAND), verdict])

    def factory(task_text, env):
        return FakeExecutor([
            {"kind": "operation", "op": "browser.navigate",
             "target": "https://example.com/", "summary": "navigate"},
            {"kind": "result", "result": {"status": "no_changes"}},
        ])

    agent = host_agent.HostAgent(_config(), connect=lambda: conn,
                                 executor_factory=factory)
    with pytest.raises(host_agent.AgentError):
        agent.run_once()
    blob = json.dumps(conn.sent)
    assert "devtools" not in blob
    assert "/json/version" not in blob
    # The agent opens no listening socket: its only transport is the outbound
    # connection it was handed, so there is nothing to publish.
    assert not hasattr(agent, "listen")
