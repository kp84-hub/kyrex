"""Integration tests for the Cloud <-> Browser Host channel.

These drive the REAL host agent (``browser-host/agent.py``) against the REAL
Cloud channel (``browser_host_channel.py``) over an in-memory duplex "wire"
(two queues), so registration, authentication, task round-trip, the approval
pause/resume handshake, disconnect recovery, fail-closed routing, isolation,
allowlist enforcement, and redaction are all exercised end to end without a
network or Chromium. The browser executor is a scripted fake.

Run: python3 -m pytest test_browser_host_channel.py
"""
import json
import os
import queue
import sys
import threading
import time

import pytest

_CLOUD = os.path.dirname(os.path.abspath(__file__))
if _CLOUD not in sys.path:
    sys.path.insert(0, _CLOUD)
_HOST = os.path.join(os.path.dirname(_CLOUD), "browser-host")
if _HOST not in sys.path:
    sys.path.insert(0, _HOST)

import agent as host_agent            # noqa: E402
import browser_host_channel as ch     # noqa: E402
import browser_hosts as bh            # noqa: E402
import bots                           # noqa: E402
import serve                          # noqa: E402

OWNER = "owner-1"
BOT = "bot-1"
BOT2 = "bot-2"
HOST = "host-1"
SECRET = "channel-test-secret"


# ── fakes ─────────────────────────────────────────────────────────────

class _Side:
    """One end of the in-memory wire."""

    def __init__(self, inbox, outbox):
        self._in, self._out = inbox, outbox
        self.closed = False

    def send(self, f):
        self._out.put(f)

    def recv(self, timeout=None):
        try:
            return self._in.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        self.closed = True


class FakeExecutor:
    """A scripted browser operator: yields frames, records decisions."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.decisions = []
        self.env = None

    def start(self):
        return None

    def read(self):
        return self._frames.pop(0) if self._frames else None

    def send(self, decision):
        self.decisions.append(decision)

    def stop(self):
        return None


class RecordingAudit:
    def __init__(self):
        self.entries = []

    def __call__(self, **kw):
        self.entries.append(kw)

    def ops(self):
        return [e.get("operation") for e in self.entries]


# ── rig ───────────────────────────────────────────────────────────────

class Rig:
    def __init__(self, tmp_path, script, *, host_allowlist=None,
                 cloud_allowlist=("example.com",), register_bot=True):
        self.tmp = tmp_path
        self.script = script
        self.host_allow = list(host_allowlist or ["example.com"])
        self.audit = RecordingAudit()
        self.manager = ch.HostManager(task_timeout=10, approval_timeout=5,
                                      audit_fn=self.audit)
        self.enroll = bh.enroll_host(OWNER, HOST, allowlist=self.host_allow)
        self.secret = self.enroll["secret"]
        bh.bind_bot(OWNER, BOT, HOST)
        if register_bot:
            bots.add_bot(BOT, "Browser Bot", "anthropic:x", str(tmp_path),
                         owner=OWNER, browser_allowlist=list(cloud_allowlist),
                         policy={"browser:*": 0}, status="running")
            bots.add_bot(BOT2, "Browser Bot 2", "anthropic:x", str(tmp_path),
                         owner=OWNER, browser_allowlist=list(cloud_allowlist),
                         policy={"browser:*": 0}, status="running")
        self.created = []
        self.sent = []
        self.conn = None
        self.channel = None
        self._stop = None

    def _factory(self, task_text, env):
        ex = FakeExecutor(self.script)
        ex.env = dict(env)
        self.created.append(ex)
        return ex

    def connect(self):
        self._stop = threading.Event()
        self.to_agent = queue.Queue()
        self.to_channel = queue.Queue()
        self.channel = self.manager.attach(
            send=lambda f: (self.sent.append(f), self.to_agent.put(f))
        )
        self.conn = _Side(self.to_agent, self.to_channel)
        config = host_agent.HostConfig(
            host_id=HOST, owner=OWNER, secret=self.secret,
            cloud_url="wss://cloud.test", allowlist=self.host_allow,
            profiles_root=str(self.tmp / "profiles"),
        )
        self.agent = host_agent.HostAgent(
            config, connect=lambda: self.conn, executor_factory=self._factory,
            stop=lambda s=None: self._stop.is_set(),
        )
        threading.Thread(target=self._pump, args=(self.channel, self.to_channel,
                                                  self._stop), daemon=True).start()
        threading.Thread(target=self.agent.run_once, daemon=True).start()
        assert _wait(lambda: self.manager.channel_for(HOST) is not None, 5), \
            "the agent never authenticated"

    def _pump(self, channel, inbox, stop):
        while not stop.is_set():
            try:
                f = inbox.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                channel.handle(f)
            except Exception:
                pass

    def disconnect(self):
        self._stop.set()
        if self.conn:
            self.conn.close()
        time.sleep(0.05)
        if self.channel is not None:
            self.manager.detach(self.channel)

    def resolve_when_pending(self, decision="APPROVED", owner=OWNER, bot=BOT):
        def run():
            end = time.time() + 5
            while time.time() < end:
                if self.channel.pending_approval(owner, bot):
                    self.channel.resolve_approval(owner, bot, decision)
                    return
                time.sleep(0.01)
        threading.Thread(target=run, daemon=True).start()

    def fps(self):
        return json.dumps(self.sent)


def _wait(pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KYREX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_SESSION_SECRET", SECRET)
    monkeypatch.delenv("KYREX_PROVIDER_SECRETS_KEY", raising=False)
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    yield tmp_path


NAV = {"kind": "operation", "op": "browser.navigate",
       "target": "https://example.com/", "summary": "navigate"}
READ = {"kind": "operation", "op": "browser.read", "target": "",
        "summary": "read page"}
CAND = '{"actions": [{"action": "navigate", "url": "https://example.com/"}]}'


def test_booking_operation_requires_a_server_issued_task_even_with_a_policy_grant(tmp_path):
    op={'kind':'operation','op':'browser.booking','target':'a'*32,'summary':'book'}
    rig=Rig(tmp_path,[op,{'kind':'result','result':{'status':'no_changes'}}])
    bots.update_bot(BOT,policy={'browser:*':0,'browser:booking':0})
    rig.connect()
    try:
        rig.manager.dispatch_browser_task(OWNER,BOT,CAND)
        assert rig.created[0].decisions==['DENY']
    finally:
        rig.disconnect()


def test_fixed_booking_authority_reaches_the_real_host_channel_and_is_consumed(tmp_path):
    import booking_store
    ident='a'*32
    op={'kind':'operation','op':'browser.booking','target':ident,'summary':'prepare'}
    rig=Rig(tmp_path,[op,{'kind':'result','result':{'status':'no_changes','mode':'booking_browser'}}])
    bots.update_bot(BOT,policy=serve.browser_preset_policy())
    rig.connect()
    try:
        store=booking_store.BookingStore()
        row=store.create(OWNER,'conversation')
        data=row['data'];data.update(url='https://example.com/book',details={'venue':'Example Grill'},
            browser_binding={'bot':BOT,'host':HOST,'allowlist':['example.com']})
        row=store.update(row,data,'preparing')
        wire=store.wire(row,BOT,HOST,'prepare')
        result=rig.manager.dispatch_browser_task(OWNER,BOT,wire)
        assert result['mode']=='booking_browser' and rig.created[0].decisions==['ALLOW']
        assert bots.get_bot(BOT)['policy']==serve.browser_preset_policy()
        with pytest.raises(booking_store.BookingError): rig.manager.dispatch_browser_task(OWNER,BOT,wire)
        store.request_cancel(OWNER,row['id'])
        assert not store.active(OWNER,BOT,HOST,wire)
    finally:
        rig.disconnect()


# ── 1. registration / authentication ──────────────────────────────────

def test_agent_registers_and_authenticates(tmp_path):
    rig = Rig(tmp_path, [NAV, {"kind": "result",
                               "result": {"status": "no_changes"}}])
    rig.connect()
    try:
        rec = bh.get_host(HOST)
        assert rec.is_available() is True
        assert rig.manager.channel_for(HOST).host_id == HOST
        assert rig.manager.channel_for(HOST).authenticated is True
        assert "browser.host" in rig.audit.ops()
    finally:
        rig.disconnect()


def test_bad_credentials_are_refused(tmp_path):
    rig = Rig(tmp_path, [])
    rig._stop = threading.Event()
    rig.to_agent = queue.Queue()
    rig.to_channel = queue.Queue()
    rig.channel = rig.manager.attach(send=lambda f: rig.to_agent.put(f))
    rig.conn = _Side(rig.to_agent, rig.to_channel)
    # The channel must actually see the hello frame for the refusal to arrive.
    threading.Thread(target=rig._pump,
                     args=(rig.channel, rig.to_channel, rig._stop),
                     daemon=True).start()
    config = host_agent.HostConfig(host_id=HOST, owner=OWNER,
                                   secret="not-the-secret",
                                   cloud_url="wss://cloud.test")
    agent = host_agent.HostAgent(config, connect=lambda: rig.conn,
                                 executor_factory=rig._factory,
                                 stop=lambda: rig._stop.is_set())
    try:
        with pytest.raises(host_agent.AuthError):
            agent.run_once()
        assert rig.channel.closed is True
        assert rig.manager.channel_for(HOST) is None    # never registered
    finally:
        rig._stop.set()


# ── 2. task round-trip ────────────────────────────────────────────────

def test_task_round_trip(tmp_path):
    rig = Rig(tmp_path, [NAV, {"kind": "result",
                               "result": {"status": "no_changes",
                                          "final_response": "hello"}}])
    rig.connect()
    try:
        result = rig.manager.dispatch_browser_task(OWNER, BOT, CAND)
        assert result["status"] == "no_changes"
        assert result["final_response"] == "hello"
        assert rig.created[0].decisions == ["ALLOW"]
        assert "browser.task" in rig.audit.ops()
    finally:
        rig.disconnect()


# ── 3. approval pause / resume ────────────────────────────────────────

_SUBMIT = {"kind": "operation", "op": "browser.submit",
           "target": "https://example.com/confirm", "summary": "submit form"}
_APPROVAL = {"kind": "approval", "tier": 2, "summary": "submit form",
             "detail": "consequential", "token": "SUBMIT ab12"}


def test_approval_pause_and_resume(tmp_path):
    rig = Rig(tmp_path, [_SUBMIT, _APPROVAL,
                         {"kind": "result", "result": {"status": "ok"}}])
    rig.connect()
    rig.resolve_when_pending("APPROVED")
    try:
        result = rig.manager.dispatch_browser_task(OWNER, BOT, CAND)
        assert result["status"] == "ok"
        assert rig.created[0].decisions == ["APPROVE", "APPROVED"]
    finally:
        rig.disconnect()


def test_approval_denied_flows_back(tmp_path):
    rig = Rig(tmp_path, [_SUBMIT, _APPROVAL,
                         {"kind": "result", "result": {"status": "no_changes"}}])
    rig.connect()
    rig.resolve_when_pending("DENIED")
    try:
        rig.manager.dispatch_browser_task(OWNER, BOT, CAND)
        assert rig.created[0].decisions == ["APPROVE", "DENIED"]
        decisions = [e.get("decision") for e in rig.audit.entries]
        assert "denied" in decisions
    finally:
        rig.disconnect()


def test_pending_approval_view_withholds_the_token(tmp_path):
    rig = Rig(tmp_path, [_SUBMIT, _APPROVAL,
                         {"kind": "result", "result": {"status": "ok"}}])
    rig.connect()
    seen = {}

    def capture():
        end = time.time() + 5
        while time.time() < end:
            view = rig.channel.pending_approval(OWNER, BOT)
            if view:
                seen.update(view)
                rig.channel.resolve_approval(OWNER, BOT, "APPROVED")
                return
            time.sleep(0.01)
    threading.Thread(target=capture, daemon=True).start()
    try:
        rig.manager.dispatch_browser_task(OWNER, BOT, CAND)
        assert seen.get("tier") == 2
        assert "token" not in seen
    finally:
        rig.disconnect()


# ── 4. fail-closed routing ────────────────────────────────────────────

def test_offline_host_fails_closed(tmp_path):
    rig = Rig(tmp_path, [NAV])
    # Never connected: no channel, and host is offline.
    with pytest.raises(bh.HostUnavailable):
        rig.manager.dispatch_browser_task(OWNER, BOT, CAND)


def test_no_bound_host_fails_closed(tmp_path):
    rig = Rig(tmp_path, [NAV])
    bh.unbind_bot(OWNER, BOT)
    # Two hosts? No — one host, but the binding is gone and there is exactly
    # one owner host, so it still resolves; add a second to force ambiguity.
    bh.enroll_host(OWNER, "host-2", allowlist=["example.com"])
    assert bh.host_for(OWNER, BOT) is None
    with pytest.raises(bh.HostUnavailable):
        rig.manager.dispatch_browser_task(OWNER, BOT, CAND)


def test_foreign_owner_bot_is_refused(tmp_path):
    rig = Rig(tmp_path, [NAV])
    with pytest.raises(ch.ChannelError):
        rig.manager.dispatch_browser_task("someone-else", BOT, CAND)


# ── 5. disconnect + heartbeat-timeout recovery ────────────────────────

def _authenticate_channel(rig, nonce, *, before_registration=None, now=None):
    sent = []
    channel = rig.manager.attach(sent.append)
    if now is not None:
        channel._now = lambda: now
    if before_registration is not None:
        register = channel.on_authenticated

        def delayed_registration():
            before_registration()
            return register()

        channel.on_authenticated = delayed_registration
    config = host_agent.HostConfig(
        host_id=HOST, owner=OWNER, secret=rig.secret,
        cloud_url="wss://cloud.test")
    channel.handle(ch.frame("hello", {
        "host_id": HOST, "owner": OWNER, "nonce": nonce,
        "proof": config.proof(nonce), "protocol": ch.PROTOCOL_VERSION,
    }))
    assert sent[0]["type"] == "hello_ok"
    return channel, sent


def test_late_disconnect_does_not_offline_replacement_or_block_dispatch(tmp_path):
    rig = Rig(tmp_path, [])
    old, _ = _authenticate_channel(rig, "old-connection")
    replacement, sent = _authenticate_channel(rig, "replacement")

    rig.manager.detach(old)

    assert rig.manager.channel_for(HOST) is replacement
    assert bh.get_host(HOST).is_available()
    send = replacement._send_raw

    def respond(f):
        send(f)
        if f["type"] == "task":
            replacement.handle(ch.frame("result", {
                "task_id": f["payload"]["task_id"],
                "result": {"status": "no_changes"},
            }))

    replacement._send_raw = respond
    assert rig.manager.dispatch_browser_task(OWNER, BOT, CAND)["status"] == "no_changes"
    assert len([f for f in sent if f["type"] == "task"]) == 1


def test_disconnect_between_hello_and_registration_preserves_new_liveness(tmp_path):
    rig = Rig(tmp_path, [])
    old, _ = _authenticate_channel(rig, "old-connection")
    replacement, _ = _authenticate_channel(
        rig, "replacement", before_registration=lambda: rig.manager.detach(old))

    assert rig.manager.channel_for(HOST) is replacement
    assert bh.get_host(HOST).is_available()


def test_replacement_retires_old_work_without_replay_or_late_heartbeats(tmp_path):
    rig = Rig(tmp_path, [])
    old, old_sent = _authenticate_channel(rig, "old-connection")
    run = ch._TaskRun("bx-old-attempt", OWNER, BOT, timeout=30)
    old._task = run
    pending = queue.Queue()
    old._messages_requests["old-message"] = pending

    replacement, sent = _authenticate_channel(rig, "replacement")

    assert old.closed
    result = old._drive(run, allowlist=[], policy={})
    assert result["status"] == "error" and "connection lost" in result["errors"][0]
    assert pending.get_nowait()["error"] == "Messages host disconnected"
    assert replacement._task is None
    assert not any(f["type"] == "task" for f in sent)
    last_seen = bh.get_host(HOST).last_seen_at
    old._now = lambda: last_seen + 3600
    old.handle(ch.frame("heartbeat", {}))
    assert bh.get_host(HOST).last_seen_at == last_seen
    old._send(ch.frame("verdict", {"task_id": run.task_id, "decision": "ALLOW"}))
    assert [f["type"] for f in old_sent] == ["hello_ok"]
    rig.manager.detach(old)
    rig.manager.detach(old)
    assert rig.manager.channel_for(HOST) is replacement
    assert bh.get_host(HOST).is_available()


def test_stale_sweep_snapshot_does_not_drop_a_fresh_replacement(tmp_path, monkeypatch):
    rig = Rig(tmp_path, [])
    old, _ = _authenticate_channel(rig, "old-connection")
    sweep_time = time.time() + bh.heartbeat_timeout() + 1
    sweep = bh.sweep_stale
    replacement = []

    def reconnect_after_scan(*, now=None):
        stale = sweep(now=now)
        replacement.append(_authenticate_channel(rig, "replacement", now=sweep_time)[0])
        return stale

    monkeypatch.setattr(bh, "sweep_stale", reconnect_after_scan)
    assert rig.manager.sweep(now=sweep_time) == []
    assert rig.manager.channel_for(HOST) is replacement[0]
    assert not replacement[0].closed
    assert bh.get_host(HOST).is_available(sweep_time)


def test_rejected_replacement_cannot_retire_authenticated_owner_connection(tmp_path):
    rig = Rig(tmp_path, [])
    current, _ = _authenticate_channel(rig, "current")
    intruder = rig.manager.attach(lambda f: None)
    intruder.handle(ch.frame("hello", {
        "host_id": HOST, "nonce": "bad-proof", "proof": "invalid",
    }))
    rig.manager.detach(intruder)
    assert intruder.closed
    assert rig.manager.channel_for(HOST) is current
    assert not current.closed
    assert bh.get_host(HOST).is_available()


def test_replacement_releases_old_approval_wait_without_approving_or_replay(tmp_path):
    rig = Rig(tmp_path, [])
    old, old_sent = _authenticate_channel(rig, "old-connection")
    run = ch._TaskRun("bx-awaiting-review", OWNER, BOT, timeout=30)
    old._task = run
    results = []
    worker = threading.Thread(target=lambda: results.append(
        old._drive(run, allowlist=[], policy={})), daemon=True)
    old.handle(ch.frame("approval", {"tier": 2, "summary": "Review exact action"}))
    worker.start()
    assert _wait(lambda: old.pending_approval(OWNER, BOT) is not None, 1)
    approval = old._pending[(OWNER, BOT)]
    try:
        replacement, sent = _authenticate_channel(rig, "replacement")
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert results[0]["status"] == "error"
        assert "connection lost" in results[0]["errors"][0]
        assert approval["decision"] == "DENIED"
        assert old.pending_approval(OWNER, BOT) is None
        assert replacement._task is None
        assert not any(f["type"] == "task" for f in sent)
        assert [f["type"] for f in old_sent] == ["hello_ok"]
        assert bh.get_host(HOST).is_available()
    finally:
        approval["decision"] = "DENIED"
        approval["event"].set()
        worker.join(timeout=2)


def test_disconnect_marks_unavailable_then_reconnect_recovers(tmp_path):
    rig = Rig(tmp_path, [NAV, {"kind": "result",
                               "result": {"status": "no_changes"}}])
    rig.connect()
    try:
        assert rig.manager.dispatch_browser_task(OWNER, BOT, CAND)["status"] \
            == "no_changes"

        rig.disconnect()
        assert bh.get_host(HOST).state == bh.STATE_UNAVAILABLE
        with pytest.raises(bh.HostUnavailable):
            rig.manager.dispatch_browser_task(OWNER, BOT, CAND)

        # The host reconnects (re-runs the handshake) and work resumes.
        rig.connect()
        assert bh.get_host(HOST).is_available() is True
        assert rig.manager.dispatch_browser_task(OWNER, BOT, CAND)["status"] \
            == "no_changes"
    finally:
        rig.disconnect()


def test_disconnect_wakes_inflight_browser_task_immediately(tmp_path):
    rig = Rig(tmp_path, [])
    rig.connect()
    try:
        channel = rig.channel
        run = ch._TaskRun("bx-disconnect", OWNER, BOT, timeout=30)
        channel._task = run

        rig.disconnect()

        result = channel._drive(run, allowlist=[], policy={})
        assert result["status"] == "error"
        assert "connection lost" in result["errors"][0]
    finally:
        rig.disconnect()


def test_stale_heartbeat_sweep_drops_the_channel(tmp_path):
    rig = Rig(tmp_path, [NAV])
    rig.connect()
    try:
        swept = rig.manager.sweep(now=time.time() + bh.heartbeat_timeout() + 1)
        assert any(h["host_id"] == HOST for h in swept)
        assert rig.manager.channel_for(HOST) is None
        with pytest.raises(bh.HostUnavailable):
            rig.manager.dispatch_browser_task(OWNER, BOT, CAND)
    finally:
        rig.disconnect()


# ── 6. allowlist enforcement (Cloud + host) ───────────────────────────

def test_cloud_allowlist_blocks_the_task_before_dispatch(tmp_path):
    rig = Rig(tmp_path, [NAV])
    bad = '{"actions": [{"action": "navigate", "url": "https://evil.com/"}]}'
    with pytest.raises(ch.ChannelError):
        rig.manager.dispatch_browser_task(OWNER, BOT, bad)


def test_cloud_denies_an_off_allowlist_operation(tmp_path):
    # The task itself targets an allowed host, but the operator proposes an
    # operation that navigates off-list: the Cloud must DENY it.
    evil_op = {"kind": "operation", "op": "browser.navigate",
               "target": "https://evil.com/", "summary": "navigate"}
    rig = Rig(tmp_path, [evil_op, {"kind": "result",
                                   "result": {"status": "no_changes"}}])
    rig.connect()
    try:
        rig.manager.dispatch_browser_task(OWNER, BOT, CAND)
        assert rig.created[0].decisions == ["DENY"]
    finally:
        rig.disconnect()


def test_host_allowlist_blocks_even_when_the_cloud_allows(tmp_path):
    # Defense in depth: the Cloud's allowlist includes evil.com, but the HOST's
    # own allowlist does not, so the host refuses to run the task.
    rig = Rig(tmp_path, [NAV, {"kind": "result",
                               "result": {"status": "no_changes"}}],
              host_allowlist=["example.com"],
              cloud_allowlist=["example.com", "evil.com"])
    rig.connect()
    try:
        bad = '{"actions": [{"action": "navigate", "url": "https://evil.com/"}]}'
        result = rig.manager.dispatch_browser_task(OWNER, BOT, bad)
        assert result["status"] == "error"
        assert "blocked on host" in json.dumps(result)
        assert rig.created == []            # the executor never ran
    finally:
        rig.disconnect()


# ── 7. per-(owner, bot) profile isolation ─────────────────────────────

def test_profile_isolation_per_owner_and_bot(tmp_path):
    rig = Rig(tmp_path, [NAV, {"kind": "result",
                               "result": {"status": "no_changes"}}])
    rig.connect()
    try:
        rig.manager.dispatch_browser_task(OWNER, BOT, CAND)
        env1 = rig.created[0].env
        assert env1["KYREX_BOT_ID"] == BOT
        assert env1["KYREX_BOT_OWNER"] == OWNER
        assert env1["KYREX_BROWSER_MANAGED"] == "1"
        d1 = env1["KYREX_BROWSER_SESSION_DIR"]
        assert d1.endswith(os.path.join(f"bot-{BOT}", f"owner-{OWNER}"))

        # A different Bot resolves to a different profile directory.
        bh.bind_bot(OWNER, BOT2, HOST)
        rig.script = [NAV, {"kind": "result",
                            "result": {"status": "no_changes"}}]
        rig.manager.dispatch_browser_task(OWNER, BOT2, CAND)
        d2 = rig.created[1].env["KYREX_BROWSER_SESSION_DIR"]
        assert d1 != d2
    finally:
        rig.disconnect()


# ── 8. redaction / no public CDP ──────────────────────────────────────

def test_cdp_url_in_a_result_is_redacted(tmp_path):
    rig = Rig(tmp_path, [NAV, {"kind": "result", "result": {
        "status": "error",
        "errors": ["failed ws://127.0.0.1:9222/devtools/browser/abc123"],
    }}])
    rig.connect()
    try:
        result = rig.manager.dispatch_browser_task(OWNER, BOT, CAND)
        blob = json.dumps(result)
        assert "devtools/browser" not in blob
        assert "127.0.0.1:9222" not in blob
        assert "[redacted-cdp]" in blob
    finally:
        rig.disconnect()


def test_no_frame_to_the_host_exposes_cdp(tmp_path):
    rig = Rig(tmp_path, [NAV, {"kind": "result",
                               "result": {"status": "no_changes"}}])
    rig.connect()
    try:
        rig.manager.dispatch_browser_task(OWNER, BOT, CAND)
        blob = rig.fps()
        for needle in ("devtools", "/json/version", "ws://127.0.0.1"):
            assert needle not in blob
    finally:
        rig.disconnect()


# ── 9. audit ──────────────────────────────────────────────────────────

class _CrashExecutor:
    """An operator that emits one progress frame, then dies with no result."""

    def __init__(self):
        self._frames = [{"kind": "progress",
                         "note": {"browser": "managed", "state": "connected"}}]

    def start(self):
        return None

    def read(self):
        return self._frames.pop(0) if self._frames else None

    def send(self, decision):
        return None

    def stop(self):
        return 3

    def stderr_tail(self):
        return "boom: [redacted-cdp] token=[redacted]"


def test_operator_crash_after_progress_is_an_immediate_terminal_failure(tmp_path):
    """Regression: no ``KYREX_RESULT_JSON`` must terminate the task AT ONCE.

    The host emits the channel ``error`` frame on EOF; the Cloud turns it into a
    terminal result immediately instead of blocking until TASK_TIMEOUT.
    """
    rig = Rig(tmp_path, [])
    rig.manager._task_timeout = 300          # would hang the test if waited out
    rig._factory = lambda task_text, env: _CrashExecutor()
    rig.connect()
    try:
        started = time.time()
        result = rig.manager.dispatch_browser_task(OWNER, BOT, CAND)
        elapsed = time.time() - started
        # The host's inbound ``error`` frame becomes a terminal error result
        # immediately — carrying the exit code, never a "timed out" placeholder.
        assert result["status"] == "error"
        errors = " ".join(result.get("errors") or [])
        assert "exit code 3" in errors, result
        assert "timed out" not in errors, result
        assert elapsed < 5, "the Cloud waited for TASK_TIMEOUT"
    finally:
        rig.disconnect()


def test_audit_records_lifecycle_and_operations(tmp_path):
    rig = Rig(tmp_path, [NAV, {"kind": "result",
                               "result": {"status": "no_changes"}}])
    rig.connect()
    try:
        rig.manager.dispatch_browser_task(OWNER, BOT, CAND)
        ops = rig.audit.ops()
        assert "browser.host" in ops
        assert "browser.task" in ops
        assert "browser.navigate" in ops
        decisions = {e["operation"]: e["decision"] for e in rig.audit.entries}
        assert decisions["browser.host"] == "allow"
        assert decisions["browser.navigate"] == "allow"
    finally:
        rig.disconnect()
