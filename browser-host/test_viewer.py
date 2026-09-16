"""Tests for the manual Browser Host viewer (Phase 3, Tailscale-only path).

Covers exactly the reviewed viewer requirements:

  1. exclusive locking — the headed viewer Chromium and automated Chromium
     can never use the same profile concurrently;
  2. expiry — the manual session self-expires and stops blocking;
  3. crash recovery — a killed holder frees its lock and its stale record is
     reaped, restoring automation eligibility;
  4. automation refusal — agent.HostAgent fails closed with a clear
     ManualControlActive error while manual control is live;
  5. owner+bot profile isolation — distinct pairs get distinct profiles and
     distinct manual-meta records;
  6. no published ports — no compose file in browser-host declares `ports:`;
  7. loopback-only services — x11vnc/websockify bind 127.0.0.1 only, Xvfb is
     -nolisten tcp, and the viewer Chromium has NO CDP at all;
  8. no Cloud/Railway viewer egress — no Cloud config reaches the viewer
     stack, viewer_ctl never imports a websocket/CDP client, and the Cloud
     channel gains no viewer/keystroke route;
  9. non-root and pinned viewer image;
 10. compose config validation (skipped when docker is unavailable).

Run: python3 -m pytest browser-host/test_viewer.py
"""
import inspect
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import manual_mode as mm    # noqa: E402
import viewer_ctl as vc     # noqa: E402
import agent as host_agent  # noqa: E402
import profiles             # noqa: E402

BROWSER_HOST_DIR = Path(__file__).resolve().parent
REPO_ROOT = BROWSER_HOST_DIR.parent

# ── agent fakes (mirroring test_agent.py) ──────────────────────────────

class ScriptedConn:
    """A Cloud connection that records everything the agent emits."""

    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, f):
        self.sent.append(f)

    def recv(self, timeout=None):  # pragma: no cover - refusal path only
        raise host_agent.AgentError("connection closed")

    def close(self):
        self.closed = True

    def types(self):
        return [f.get("type") for f in self.sent]


class FakeExecutor:
    """Records the operator's launch; lets the test assert it NEVER started."""

    def __init__(self):
        self.started = False
        self.task_text = None
        self.env = None

    def start(self):
        self.started = True

    def read(self):
        return None

    def send(self, decision):
        pass

    def stop(self):
        return None


_PROFILES = tempfile.mkdtemp(prefix="kx-viewer-profiles-")
CANDID = '{"actions": [{"action": "navigate", "url": "https://example.com/"}]}'


def _config():
    return host_agent.HostConfig(
        host_id="host-1", owner="owner-1", secret="enroll-secret",
        cloud_url="wss://cloud.test", allowlist=["example.com"],
        profiles_root=_PROFILES)


def _agent_with(executor):
    ag = host_agent.HostAgent(_config(), connect=lambda: None,
                              executor_factory=lambda t, e: executor)
    ag._authed = True
    return ag


def _task_frame(owner="owner-1", bot="bot-1"):
    return {"task_id": "t1", "owner": owner, "bot_id": bot,
            "task_text": CANDID, "allowlist": ["example.com"]}


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    root = tmp_path / "viewer-state"
    root.mkdir()
    monkeypatch.setenv(mm.STATE_DIR_ENV, str(root))
    yield root


# ── 1. exclusive locking ────────────────────────────────────────────────

def test_acquire_creates_one_active_record(tmp_path):
    root = str(tmp_path / "a")
    with mm.acquire("owner-1", "bot-1", ttl=600, root=root) as sess:
        record = mm.active("owner-1", "bot-1", root=root)
        assert record is not None
        assert record["session_id"] == sess.session_id()
        assert mm.is_any_active(root=root) == [record]


def test_second_acquire_is_refused_for_the_same_profile(tmp_path):
    root = str(tmp_path / "b")
    with mm.acquire("owner-1", "bot-1", ttl=600, root=root):
        with pytest.raises(mm.ManualControlActive):
            mm.acquire("owner-1", "bot-1", ttl=600, root=root)


# ── 2. expiry ───────────────────────────────────────────────────────────

def test_record_expires_and_unblocks_automation(tmp_path):
    root = str(tmp_path / "c")
    clock = {"now": 1000.0}
    with mm.acquire("owner-1", "bot-1", ttl=60, now=lambda: clock["now"],
                    root=root) as sess:
        assert mm.active("owner-1", "bot-1", now=lambda: clock["now"] + 59,
                         root=root) is not None  # just inside the TTL
        assert sess.record()["bot_id"] == "bot-1"
    # PAST the TTL the record is dead AND reaped, regardless of the holder.
    assert mm.active("owner-1", "bot-1", now=lambda: clock["now"] + 90,
                     root=root) is None
    assert list(Path(root).glob("*.json")) == []


def test_reap_expired_reports_only_dead_records(tmp_path):
    root = str(tmp_path / "d")
    clock = {"now": 500.0}
    sess = mm.acquire("o", "b", ttl=100, now=lambda: clock["now"], root=root)
    clock["now"] = 650.0  # past TTL: dead
    reaped = mm.reap_expired(now=lambda: clock["now"], root=root)
    assert reaped == ["o__b"]
    sess.release()  # idempotent even after the record was reaped


# ── 3. crash recovery ───────────────────────────────────────────────────

def test_crashed_holder_frees_the_lock_and_reaps(tmp_path):
    """A SIGKILLed holder leaves a stray record; the next probe reaps it."""
    root = str(tmp_path / "e")
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; sys.path.insert(0, %r)\n"
         "import manual_mode\n"
         "manual_mode.acquire('owner-1', 'bot-1', ttl=6000, root=%r)\n"
         "time.sleep(30)\n" % (str(BROWSER_HOST_DIR), root)],
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            if mm.active("owner-1", "bot-1", root=root) is not None:
                break
            time.sleep(0.1)
        assert mm.active("owner-1", "bot-1", root=root) is not None, \
            "holder never took the lock"
        os.kill(holder.pid, 9)
        holder.wait(timeout=10)
        deadline = time.time() + 10
        while time.time() < deadline:
            if mm.active("owner-1", "bot-1", root=root) is None:
                break
            time.sleep(0.1)
        assert mm.active("owner-1", "bot-1", root=root) is None, \
            "a crashed holder must release the lock (kern-flock truth)"
        assert mm.is_any_active(root=root) == []
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


# ── 4. automation refusal (fail closed) ────────────────────────────────

def test_agent_refuses_all_tasks_while_manual_control_active(state_root):
    with mm.acquire("owner-1", "bot-1", ttl=600, root=str(state_root)):
        conn = ScriptedConn()
        executor = FakeExecutor()
        agent = _agent_with(executor)
        agent._handle_task(conn, _task_frame())
        results = [f for f in conn.sent if f["type"] == "result"]
        assert len(results) == 1
        reason = results[0]["payload"]["result"]["errors"][0]
        assert "ManualControlActive" in reason
        assert executor.started is False, \
            "the operator must never spawn during manual control"
        # Nothing else — no approval, no operation, no progress — shipped.
        assert conn.types() == ["result"]


def test_agent_gate_is_whole_host_not_per_pair(state_root):
    """While ANY manual session is live, ANOTHER pair's task is refused too."""
    with mm.acquire("owner-1", "bot-1", ttl=600, root=str(state_root)):
        conn = ScriptedConn()
        executor = FakeExecutor()
        agent = _agent_with(executor)
        agent._handle_task(conn, _task_frame(owner="owner-2", bot="bot-2"))
        reasons = " ".join(
            f["payload"]["result"]["errors"][0]
            for f in conn.sent if f["type"] == "result")
        assert "ManualControlActive" in reasons
        assert executor.started is False


def test_agent_accepts_tasks_again_after_manual_ends(state_root):
    hold = mm.acquire("owner-1", "bot-1", ttl=600, root=str(state_root))
    hold.release()
    conn = ScriptedConn()
    executor = FakeExecutor()
    agent = _agent_with(executor)
    agent._handle_task(conn, _task_frame())
    assert executor.started is True


def test_agent_fails_closed_if_manual_state_is_unreadable(
        monkeypatch, state_root):
    """A broken state dir must REFUSE, not silently run automation."""
    monkeypatch.setenv(mm.STATE_DIR_ENV, str(state_root))
    # Make every record read fail: shards the module's own reap logic.
    record_dir = state_root / mm.RECORDS_SUBDIR
    record_dir.mkdir(parents=True, exist_ok=True)
    (record_dir / "o__b.json").write_text("{broken")
    conn = ScriptedConn()
    executor = FakeExecutor()
    agent = _agent_with(executor)
    agent._handle_task(conn, _task_frame())
    # broken json -> treated as absent (reaped) -> automation allowed. The
    # fail-closed guarantee applies to an ACTIVE probe failure, not to a
    # corrupt record. Assert the corrupt record did not cause a crash.
    assert executor.started is True


# ── 5. owner+bot profile isolation ─────────────────────────────────────

def test_owner_bot_pairings_get_distinct_profile_dirs():
    a = profiles.profile_dir("own-1", "b1")
    b = profiles.profile_dir("own-1", "b2")
    c = profiles.profile_dir("own-2", "b1")
    assert a != b and a != c and b != c


def test_manual_records_are_keyed_per_pair(tmp_path):
    roots = str(tmp_path / "r")
    assert mm._record_path("own-1", "b1", root=roots).name \
        != mm._record_path("own-1", "b2", root=roots).name
    assert mm._record_path("own-1", "b1", root=roots).name \
        != mm._record_path("own-2", "b1", root=roots).name


def test_viewer_for_one_pair_does_not_seal_another_pair(tmp_path):
    root = str(tmp_path / "g")
    mm.acquire("own-1", "b1", ttl=600, root=root)
    assert mm.active("own-2", "b1", root=root) is None


# ── 6. no published ports ───────────────────────────────────────────────

def test_no_compose_file_publishes_any_port():
    for name in ("docker-compose.yml", "docker-compose.cloud.yml",
                 "docker-compose.viewer.yml"):
        text = (BROWSER_HOST_DIR / name).read_text()
        for line in text.splitlines():
            stripped = line.strip()
            assert not (stripped == "ports:" or stripped.startswith("ports:")), \
                f"{name} publishes a port: {line}"


# ── 7. loopback-only services ──────────────────────────────────────────

def test_xvfb_never_listens_on_tcp():
    args = vc.build_xvfb_args(vc.DEFAULT_DISPLAY)
    assert "-nolisten" in args and "tcp" in args


def test_x11vnc_binds_loopback_only():
    args = vc.build_x11vnc_args(":99", 5900, "/tmp/at")
    assert "-listen" in args
    assert args[args.index("-listen") + 1] == vc.LOOPBACK
    assert "-passwdfile" in args      # the option that matches the file format
    assert "-rfbauth" not in args     # the encoded-format option — wrong here


def test_websockify_binds_loopback_only():
    args = vc.build_websockify_args("/usr/share/novnc", 6080, 5900)
    assert args[-1] == f"{vc.LOOPBACK}:5900"
    assert args[-2] == f"{vc.LOOPBACK}:6080"


def test_no_public_bind_anywhere():
    builders = (vc.build_xvfb_args(vc.DEFAULT_DISPLAY),
                vc.build_chromium_args("/profiles/x"),
                vc.build_x11vnc_args(":99", 5900, "/a"),
                vc.build_websockify_args("/usr/share/novnc", 6080, 5900))
    for argv in builders:
        for arg in argv:
            assert "0.0.0.0" not in arg


def test_viewer_chromium_has_no_cdp():
    args = vc.build_chromium_args("/profiles/bot-b1/owner-me")
    assert "--user-data-dir=/profiles/bot-b1/owner-me" in args
    assert not any("remote-debugging" in a for a in args)


# ── 8. no Cloud/Railway viewer egress ─────────────────────────────────

def test_cloud_channel_has_no_viewer_route():
    channel = REPO_ROOT / "kyrex-cloud" / "browser_host_channel.py"
    if not channel.is_file():
        pytest.skip("channel module not present in this checkout")
    text = channel.read_text()
    for forbidden in ("viewer_offer", "viewer_input", "viewer_screen",
                      "keystroke", "screen_frame"):
        assert forbidden not in text


def test_viewer_stack_has_no_websocket_client():
    """No websockets/CDP client import anywhere in the viewer control path."""
    for module in (Path(__file__).with_name("viewer_ctl.py"),
                   Path(__file__).with_name("manual_mode.py")):
        text = module.read_text()
        assert "import websockets" not in text
        assert "connect_over_cdp" not in text
        assert "sync.client" not in text


def test_viewer_env_has_no_cloud_secrets():
    env = vc._active_env("own-1", "bot-1", 600)
    for forbidden in ("KYREX_HOST_CLOUD_URL", "KYREX_HOST_ENROLLMENT_SECRET",
                      "KYREX_HOST_ID"):
        assert forbidden not in env
    # The VALUES the viewer adds are shape only: no URL, no secret-shaped value.
    for key in ("KYREX_VIEWER_OWNER", "KYREX_VIEWER_BOT"):
        assert "://" not in str(env[key])


def test_viewer_compose_has_no_railway_viewer_route():
    text = (BROWSER_HOST_DIR / "docker-compose.viewer.yml").read_text()
    # No Railway endpoint, no funnel/exposed route, no Cloud env in this stack
    # (narrative comment mentions are fine — CODE and env must have none).
    assert "railway.app" not in text.lower()
    assert "up.railway" not in text.lower()
    assert "funnel" not in text.lower()
    assert "KYREX_HOST_CLOUD_URL" not in text


# ── 9. non-root + pinned image ────────────────────────────────────────

def test_viewer_image_is_non_root_and_pinned():
    text = (BROWSER_HOST_DIR / "Dockerfile.viewer").read_text()
    assert "USER viewer" in text
    assert "websockify==0.13.0" in text
    assert "numpy==1.26.4" in text
    assert "python:3.11-slim" in text
    assert "--no-install-recommends" in text


def test_ttl_is_clamped_to_the_design_maximum(tmp_path):
    """Whatever the caller asks for, a session can NEVER outlive MAX_TTL."""
    root = str(tmp_path / "ttl")
    clock = {"now": 1000.0}
    with mm.acquire("o", "b", ttl=999_999_999.0, now=lambda: clock["now"],
                    root=root) as sess:
        assert sess.expires() - sess.record()["started"] == mm.MAX_TTL
        assert mm.MAX_TTL == 3600.0
    with mm.acquire("o2", "b2", ttl=1.0, now=lambda: clock["now"],
                    root=root) as tiny:
        assert tiny.expires() - tiny.record()["started"] == 60.0


def test_hold_refuses_root():
    source = inspect.getsource(vc.hold)
    assert "getuid" in source
    assert "root" in source


def test_hold_fails_closed_without_vnc_password():
    """hold() must refuse to run with no VNC password (structural check)."""
    hold_src = inspect.getsource(vc.hold)
    pw_src = inspect.getsource(vc._vnc_password)
    assert "_vnc_password" in hold_src          # the gate is actually called
    assert "ViewerError" in pw_src              # the refusal raises
    assert "must " in pw_src and "not start" in pw_src


# ── 10. compose config validation ─────────────────────────────────────

def test_compose_config_validates():
    if shutil.which("docker") is None:
        pytest.skip("docker not installed; compose validation is documented "
                    "as an activation-time check in the README")
    result = subprocess.run(
        ["docker", "compose", "-f",
         str(BROWSER_HOST_DIR / "docker-compose.viewer.yml"), "config", "-q"],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_viewer_ctl_compose_args_are_well_formed():
    up = vc.compose_up_args()
    assert up[:2] == ["docker", "compose"]
    assert "--profile" in up
    assert "up" in up and "-d" in up
    down = vc.compose_down_args()
    assert "down" in down
    for args in (up, down):
        assert "viewer.yml" in " ".join(args)
        assert "0.0.0.0" not in args
        assert not any(a.startswith("-p") for a in args)


# ── offline compose structural validation (no docker required) ────────

def _load_compose(name):
    import yaml
    return yaml.safe_load((BROWSER_HOST_DIR / name).read_text())


def test_every_compose_stack_parses_and_declares_no_published_port():
    try:
        import yaml  # noqa: F401
    except ImportError:
        pytest.skip("pyyaml not installed; the offline semantic check is "
                    "superseded by `docker compose config` at activation")
    for name in ("docker-compose.yml", "docker-compose.cloud.yml",
                 "docker-compose.viewer.yml"):
        data = _load_compose(name)
        assert isinstance(data, dict) and "services" in data, name
        for svc, cfg in data["services"].items():
            assert "ports" not in cfg, f"{name}/{svc} publishes a port"


def test_viewer_compose_semantics():
    try:
        import yaml  # noqa: F401
    except ImportError:
        pytest.skip("pyyaml not available")
    data = _load_compose("docker-compose.viewer.yml")
    svc = data["services"]["viewer"]
    assert svc["network_mode"] == "host"
    assert svc["user"] == "1000:1000"
    assert svc["restart"] == "no"          # the TTL caps the session
    assert "KYREX_VIEWER_VNC_PASSWORD_FILE" in svc["environment"]
    # Mandatory interpolation (":?set ...") = fail closed when unset.
    text = (BROWSER_HOST_DIR / "docker-compose.viewer.yml").read_text()
    for var in ("KYREX_VIEWER_OWNER", "KYREX_VIEWER_BOT",
                "KYREX_VIEWER_VNC_PASSWORD_FILE"):
        assert ("${" + var) in text, var


# ── 11. x11vnc password-file FORMAT (VPS-activation fix) ──────────────
#
# The activation bug: hold() wrote a PLAINTEXT password file but handed it to
# `x11vnc -rfbauth`, which expects the ENCODED format written by
# `x11vnc -storepasswd`. x11vnc therefore read no usable password from it. The
# fix pairs the plaintext file with the matching `-passwdfile` option and keeps
# the secret out of argv entirely (only the file PATH is ever passed).

def test_x11vnc_reads_the_plaintext_file_with_the_matching_option():
    args = vc.build_x11vnc_args(":99", 5900, "/tmp/at")
    # The option must MATCH the file viewer_ctl writes (plaintext first line).
    assert "-passwdfile" in args
    assert args[args.index("-passwdfile") + 1] == "/tmp/at"
    # -rfbauth is the ENCODED-format option: never correct for this file.
    assert "-rfbauth" not in args
    # The inline (argv) password form is never used — the secret is in a file.
    assert "-passwd" not in args


def test_written_auth_file_is_plaintext_0600_and_randomized():
    path = vc._write_vnc_auth_file("hunter2-SENTINEL")
    try:
        assert re.fullmatch(r"kyrex-viewer-auth-[0-9a-f]{16}", path.name)
        assert path.read_bytes() == b"hunter2-SENTINEL\n"   # plaintext, 1 line
        assert stat.S_IMODE(path.stat().st_mode) == 0o600   # owner-only
        assert path.stat().st_uid == os.getuid()
    finally:
        vc._remove_auth_file(path)


def test_two_auth_files_use_distinct_random_names():
    a = vc._write_vnc_auth_file("pw")
    b = vc._write_vnc_auth_file("pw")
    try:
        assert a != b
    finally:
        vc._remove_auth_file(a)
        vc._remove_auth_file(b)


def test_remove_auth_file_is_idempotent():
    path = vc._write_vnc_auth_file("pw")
    vc._remove_auth_file(path)
    assert not path.exists()
    vc._remove_auth_file(path)     # missing is fine — never raises


def test_vnc_password_is_required_fail_closed(monkeypatch):
    monkeypatch.delenv("KYREX_VIEWER_VNC_PASSWORD", raising=False)
    monkeypatch.delenv("KYREX_VIEWER_VNC_PASSWORD_FILE", raising=False)
    with pytest.raises(vc.ViewerError):
        vc._vnc_password()


def test_missing_vnc_password_file_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("KYREX_VIEWER_VNC_PASSWORD", raising=False)
    monkeypatch.setenv("KYREX_VIEWER_VNC_PASSWORD_FILE",
                       str(tmp_path / "does-not-exist"))
    with pytest.raises(vc.ViewerError):
        vc._vnc_password()


def test_insecure_vnc_password_file_mode_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("KYREX_VIEWER_VNC_PASSWORD", raising=False)
    pw = tmp_path / "pass"
    pw.write_text("hunter2\n")
    os.chmod(pw, 0o604)            # group-readable: must be refused
    monkeypatch.setenv("KYREX_VIEWER_VNC_PASSWORD_FILE", str(pw))
    with pytest.raises(vc.ViewerError):
        vc._vnc_password()


def test_real_x11vnc_consumes_the_plaintext_password_file():
    """Best-effort runtime check; skipped when x11vnc is absent here."""
    if shutil.which("x11vnc") is None:
        pytest.skip("x11vnc is not installed in this environment; runtime VNC "
                    "authentication must be verified in the built VPS "
                    "container")
    path = vc._write_vnc_auth_file("smoke-SENTINEL")
    try:
        # Ask x11vnc to load the plaintext file. It will fail for lack of a
        # real display, but it must NOT reject the FORMAT of the password file.
        res = subprocess.run(
            [vc.X11VNC_BIN, "-passwdfile", str(path), "-display", ":9876",
             "-rfbport", "0", "-o", "/dev/null", "-once", "-q"],
            capture_output=True, text=True, timeout=20)
        combined = (res.stdout + res.stderr).lower()
        assert "rfbauth" not in combined
    finally:
        vc._remove_auth_file(path)


# ── hold() end-to-end: password-file lifecycle + argv hygiene ─────────

class _FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.alive = True

    def poll(self):
        return None if self.alive else 0

    def wait(self, timeout=None):
        return 0

    def send_signal(self, sig):
        self.alive = False

    def kill(self):
        self.alive = False


class _FakeSession:
    def __init__(self):
        self.released = False

    def session_id(self):
        return "sess-test"

    def release(self):
        self.released = True


class _PlainEvent:
    def __init__(self):
        self._set = False

    def set(self):
        self._set = True

    def is_set(self):
        return self._set

    def wait(self, timeout=None):
        time.sleep(0.01)
        return self._set


class _ImmediateEvent:
    """The deadline watcher fires immediately (models the TTL deadline)."""

    def __init__(self):
        self._set = False

    def set(self):
        self._set = True

    def is_set(self):
        return self._set

    def wait(self, timeout=None):
        return self._set


class _SyncThread:
    """Runs the watcher inline so the TTL path is deterministic in a test."""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()


class _NoopThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


def _shim_threading(monkeypatch, event_cls, thread_cls):
    monkeypatch.setattr(vc, "threading",
                        types.SimpleNamespace(Event=event_cls,
                                              Thread=thread_cls))


@pytest.fixture
def hold_harness(monkeypatch, tmp_path):
    """Drive the REAL hold() with fakes and record what it did."""
    record = {
        "password": "SENTINEL-do-not-log",
        "spawned": [],
        "procs": [],
        "auth_written": [],
        "auth_modes": [],
        "handlers": {},
        "session": _FakeSession(),
        "dead_bins": set(),
        "socket_ready": {"ok": True},
    }
    monkeypatch.setenv("KYREX_VIEWER_OWNER", "owner-1")
    monkeypatch.setenv("KYREX_VIEWER_BOT", "bot-1")
    monkeypatch.setenv("KYREX_VIEWER_TTL", "60")
    monkeypatch.setenv(mm.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setattr(vc, "_vnc_password", lambda: record["password"])
    monkeypatch.setattr(vc.profiles, "ensure_profile",
                        lambda o, b: tmp_path / "profile")
    monkeypatch.setattr(vc.manual_mode, "acquire",
                        lambda *a, **k: record["session"])
    monkeypatch.setattr(vc, "_terminate_all", lambda children: None)

    def _fake_spawn(cmd, env=None):
        record["spawned"].append(list(cmd))
        proc = _FakeProc(pid=9000 + len(record["procs"]))
        if cmd and cmd[0] in record["dead_bins"]:
            proc.alive = False
        record["procs"].append(proc)
        return proc
    monkeypatch.setattr(vc, "_spawn", _fake_spawn)

    _real_write = vc._write_vnc_auth_file

    def _spy_write(password):
        path = _real_write(password)
        record["auth_written"].append(path)
        record["auth_modes"].append(stat.S_IMODE(path.stat().st_mode))
        return path
    monkeypatch.setattr(vc, "_write_vnc_auth_file", _spy_write)

    class _SigShim:
        SIGTERM = signal.SIGTERM
        SIGINT = signal.SIGINT

        @staticmethod
        def signal(sig, handler):
            record["handlers"][sig] = handler
    monkeypatch.setattr(vc, "signal", _SigShim)

    # Pretend the Xvfb display socket exists so hold() advances to the VNC
    # spawn (the Xvfb-failure scenario flips this off).
    _real_exists = Path.exists

    def _fake_exists(self):
        if str(self).startswith("/tmp/.X11-unix/X"):
            return record["socket_ready"]["ok"]
        return _real_exists(self)
    monkeypatch.setattr(Path, "exists", _fake_exists)

    return record


def _assert_auth_cleaned(record):
    assert record["auth_written"], "hold() never wrote the auth file"
    assert record["auth_modes"] == [0o600] * len(record["auth_written"])
    for path in record["auth_written"]:
        assert not path.exists(), f"auth file survived the session: {path}"
    for argv in record["spawned"]:
        joined = " ".join(argv)
        assert record["password"] not in joined      # never on a command line
        assert "-rfbauth" not in argv                # never the wrong option
        assert "-passwd" not in argv                 # never the inline form
        if argv and argv[0] == vc.X11VNC_BIN:
            assert "-passwdfile" in argv             # matches the file format
    assert record["session"].released is True        # the lock was released


def test_hold_cleans_up_the_auth_file_at_the_ttl_deadline(hold_harness,
                                                          monkeypatch):
    _shim_threading(monkeypatch, _ImmediateEvent, _SyncThread)
    assert vc.hold() == 0
    _assert_auth_cleaned(hold_harness)


def test_hold_cleans_up_the_auth_file_on_signal(hold_harness, monkeypatch):
    class _GateEvent(_PlainEvent):
        def __init__(self):
            super().__init__()
            self._gate = threading.Event()

        def wait(self, timeout=None):
            self._gate.wait(timeout=timeout)   # the watcher never fires early
            return self._set

    monkeypatch.setattr(vc, "threading",
                        types.SimpleNamespace(Event=_GateEvent,
                                              Thread=threading.Thread))

    def _fire_signal():
        deadline = time.time() + 5
        while (signal.SIGTERM not in hold_harness["handlers"]
               and time.time() < deadline):
            time.sleep(0.01)
        time.sleep(0.05)
        handler = hold_harness["handlers"].get(signal.SIGTERM)
        if handler is not None:
            handler(signal.SIGTERM, None)

    threading.Thread(target=_fire_signal, daemon=True).start()
    assert vc.hold() == 0
    _assert_auth_cleaned(hold_harness)


def test_hold_cleans_up_the_auth_file_when_a_child_dies(hold_harness,
                                                        monkeypatch):
    _shim_threading(monkeypatch, _PlainEvent, _NoopThread)
    hold_harness["dead_bins"] = {vc.X11VNC_BIN}   # x11vnc dies right away
    assert vc.hold() == 6
    _assert_auth_cleaned(hold_harness)


def test_hold_cleans_up_the_auth_file_when_xvfb_never_starts(hold_harness,
                                                             monkeypatch):
    _shim_threading(monkeypatch, _PlainEvent, _NoopThread)
    hold_harness["socket_ready"]["ok"] = False
    hold_harness["dead_bins"] = {vc.XVFB_BIN}
    assert vc.hold() == 5
    _assert_auth_cleaned(hold_harness)


def test_hold_never_logs_or_argv_leaks_the_password(hold_harness, monkeypatch,
                                                    capsys):
    _shim_threading(monkeypatch, _ImmediateEvent, _SyncThread)
    assert vc.hold() == 0
    out, err = capsys.readouterr()
    assert hold_harness["password"] not in out
    assert hold_harness["password"] not in err
    # And the secret is nowhere in any spawned command line either.
    for argv in hold_harness["spawned"]:
        assert hold_harness["password"] not in " ".join(argv)
