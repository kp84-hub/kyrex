"""Regressions for the shared-lock Browser Host viewer boundary (Phase 3).

These prove the fixes for the Tailscale viewer security review:

  * automation holds the SAME per-(owner, bot) kernel flock for the FULL task
    lifetime (approvals, subprocess shutdown, terminal result) — not a lone
    check-then-start;
  * viewer-during-task refusal AND task-during-viewer refusal — both TOCTOU
    directions closed by the lock itself;
  * the shared lock/state location is IDENTICAL for the agent and the viewer
    containers (same path on the shared profiles volume);
  * containment: traversal and symlink escape are refused;
  * the corrected compose command actually runs ``viewer_ctl.py hold``;
  * SIGTERM / SIGKILL of the holder frees the lock and reaps the record;
  * TTL/teardown terminates the whole X/Chromium/VNC child process GROUP;
  * the VNC password file is git-ignored and must be mode 0600;
  * listeners stay loopback-only, no CDP, and no Cloud/Railway viewer route.

Run: python3 -m pytest browser-host/test_viewer_lock.py
"""
import inspect
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import manual_mode as mm      # noqa: E402
import viewer_ctl as vc       # noqa: E402
import agent as host_agent    # noqa: E402

BROWSER_HOST_DIR = Path(__file__).resolve().parent
REPO_ROOT = BROWSER_HOST_DIR.parent


# ── fakes ─────────────────────────────────────────────────────────────

class _Conn:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, f):
        self.sent.append(f)

    def recv(self, timeout=None):  # pragma: no cover - refusal paths only
        raise host_agent.AgentError("connection closed")

    def close(self):
        self.closed = True

    def types(self):
        return [f.get("type") for f in self.sent]


class _ProbeExecutor:
    """Records what it can see about the lock while the agent runs it."""

    def __init__(self):
        self.started = False
        self.refused_while_running = None
        self.held_at_stop = None

    def start(self):
        self.started = True

    def read(self):
        # Called INSIDE the task loop, while the agent holds the lock: a manual
        # acquire for the same profile must be refused right now.
        try:
            with mm.acquire("owner-1", "bot-1", kind=mm.KIND_MANUAL):
                self.refused_while_running = False
        except mm.ManualControlActive:
            self.refused_while_running = True
        return None

    def send(self, decision):
        pass

    def stop(self):
        # Called during subprocess shutdown, still inside the held-lock window.
        self.held_at_stop = mm.active("owner-1", "bot-1") is not None
        return 0


def _agent(tmp_path, executor):
    cfg = host_agent.HostConfig(
        host_id="h1", owner="owner-1", secret="s", cloud_url="wss://c",
        allowlist=["example.com"],
        profiles_root=str(tmp_path / "profiles"))
    ag = host_agent.HostAgent(cfg, connect=lambda: None,
                              executor_factory=lambda t, e: executor)
    ag._authed = True
    return ag


def _task():
    return {"task_id": "t1", "owner": "owner-1", "bot_id": "bot-1",
            "task_text": ('{"actions":[{"action":"navigate",'
                          '"url":"https://example.com/"}]}'),
            "allowlist": ["example.com"]}


@pytest.fixture
def shared_env(tmp_path, monkeypatch):
    root = tmp_path / "state"
    monkeypatch.setenv(mm.STATE_DIR_ENV, str(root))
    return root


# ── 1. full-lifetime automation lock ownership ────────────────────────

def test_automation_holds_the_lock_for_the_full_task(shared_env, tmp_path):
    ex = _ProbeExecutor()
    conn = _Conn()
    _agent(tmp_path, ex)._handle_task(conn, _task())

    assert ex.started is True
    # While the task ran, a manual acquire for the SAME profile was refused.
    assert ex.refused_while_running is True
    # Still held during executor shutdown (subprocess teardown).
    assert ex.held_at_stop is True
    # Released only AFTER the task returned; the record is gone.
    assert mm.active("owner-1", "bot-1") is None
    assert mm.is_any_active() == []


def test_automation_lock_is_released_when_preflight_blocks(shared_env, tmp_path):
    # A task blocked on the host allowlist must not leak the lock it took.
    conn = _Conn()
    agent = _agent(tmp_path, _ProbeExecutor())
    payload = _task()
    payload["task_text"] = ('{"actions":[{"action":"navigate",'
                            '"url":"https://evil.com/"}]}')
    agent._handle_task(conn, payload)
    assert mm.is_any_active() == []
    assert "blocked on host" in json.dumps(conn.sent)


# ── 2. both TOCTOU directions ─────────────────────────────────────────

def test_viewer_refuses_while_automation_owns_the_profile(shared_env):
    lock = mm.acquire("owner-1", "bot-1", kind=mm.KIND_AUTOMATION, ttl=600)
    try:
        with pytest.raises(mm.ManualControlActive):
            mm.acquire("owner-1", "bot-1", kind=mm.KIND_MANUAL, ttl=600)
    finally:
        lock.release()


def test_agent_refuses_while_viewer_owns_before_starting(shared_env, tmp_path):
    viewer = mm.acquire("owner-1", "bot-1", kind=mm.KIND_MANUAL, ttl=600)
    try:
        ex = _ProbeExecutor()
        conn = _Conn()
        _agent(tmp_path, ex)._handle_task(conn, _task())
        assert ex.started is False                    # nothing was started
        assert conn.types() == ["result"]             # exactly one refusal
        assert "ManualControlActive" in json.dumps(conn.sent)
    finally:
        viewer.release()


def test_default_acquire_is_manual_kind(shared_env):
    with mm.acquire("o", "b", root=str(shared_env)) as sess:
        assert sess.kind() == mm.KIND_MANUAL
        assert mm.is_any_active(kind=mm.KIND_MANUAL)[0]["kind"] == "manual"
        assert mm.is_any_active(kind=mm.KIND_AUTOMATION) == []


def test_automation_kind_is_excluded_from_the_manual_scan(shared_env):
    with mm.acquire("o", "b", kind=mm.KIND_AUTOMATION, root=str(shared_env)):
        assert mm.is_any_active(kind=mm.KIND_MANUAL) == []
        assert mm.is_any_active(kind=mm.KIND_AUTOMATION) != []
        assert mm.is_any_active() != []                # unfiltered sees it


# ── 3. cross-container shared path ────────────────────────────────────

def test_both_compose_stacks_share_one_state_dir_on_one_volume():
    yaml = pytest.importorskip("yaml")
    viewer = yaml.safe_load(
        (BROWSER_HOST_DIR / "docker-compose.viewer.yml").read_text())
    cloud = yaml.safe_load(
        (BROWSER_HOST_DIR / "docker-compose.cloud.yml").read_text())
    v_env = viewer["services"]["viewer"]["environment"]
    c_env = cloud["services"]["agent"]["environment"]
    assert v_env["KYREX_VIEWER_STATE_DIR"] == c_env["KYREX_VIEWER_STATE_DIR"]
    assert v_env["KYREX_VIEWER_STATE_DIR"] == f"/profiles/{mm.STATE_DIR_NAME}"
    # Same shared volume, same container path in both.
    assert "./profiles:/profiles" in viewer["services"]["viewer"]["volumes"]
    assert "./profiles:/profiles" in cloud["services"]["agent"]["volumes"]


def test_state_dir_derives_from_the_profiles_volume(tmp_path, monkeypatch):
    monkeypatch.delenv(mm.STATE_DIR_ENV, raising=False)
    monkeypatch.delenv(mm.PROFILES_ROOT_ENV, raising=False)
    base = tmp_path / "profroot"
    assert mm.state_dir(profiles_root=str(base)) == base / mm.STATE_DIR_NAME
    # The explicit env wins over the derived path (agent and viewer agree via env).
    other = tmp_path / "explicit"
    monkeypatch.setenv(mm.STATE_DIR_ENV, str(other))
    assert mm.state_dir(profiles_root=str(base)) == other


def test_records_live_under_the_state_root(monkeypatch, tmp_path):
    monkeypatch.delenv(mm.STATE_DIR_ENV, raising=False)
    rec = mm._record_path("owner-1", "bot-1", root=str(tmp_path))
    assert rec.parent.name == mm.RECORDS_SUBDIR
    assert str(tmp_path) in str(rec)


# ── 4. containment: traversal + symlink escape ────────────────────────

def test_traversal_components_cannot_escape(shared_env):
    rec = mm._record_path("../../etc", "..\\..", root=str(shared_env))
    assert ".." not in rec.parts
    assert str(shared_env.resolve()) in str(rec.resolve())


def test_symlinked_records_dir_is_refused(shared_env, tmp_path):
    state = mm.state_dir(root=str(shared_env))
    outside = tmp_path / "outside"
    outside.mkdir()
    records = state / mm.RECORDS_SUBDIR
    if records.exists() or records.is_symlink():
        records.unlink()
    os.symlink(outside, records)
    with pytest.raises(mm.ViewerPathError):
        mm.acquire("owner-1", "bot-1", root=str(shared_env))
    assert list(outside.iterdir()) == []          # nothing written outside


def test_symlinked_lock_file_is_refused(shared_env, tmp_path):
    state = mm.state_dir(root=str(shared_env))
    locks = state / mm.LOCKS_SUBDIR
    locks.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "victim"
    victim.write_text("do not clobber")
    key = f"{mm._slug('owner-1')}__{mm._slug('bot-1')}.lock"
    os.symlink(victim, locks / key)
    with pytest.raises(mm.ViewerPathError):
        mm.acquire("owner-1", "bot-1", root=str(shared_env))
    assert victim.read_text() == "do not clobber"  # symlink target untouched


# ── 5. corrected compose command ──────────────────────────────────────

def test_viewer_compose_runs_the_image_hold_entrypoint():
    yaml = pytest.importorskip("yaml")
    svc = yaml.safe_load(
        (BROWSER_HOST_DIR / "docker-compose.viewer.yml").read_text())
    svc = svc["services"]["viewer"]
    # The bug: an entrypoint override WITH NO command started nothing.
    assert svc.get("entrypoint") is None, "must not override the image entrypoint"
    assert svc.get("command") is None
    dockerfile = (BROWSER_HOST_DIR / "Dockerfile.viewer").read_text()
    assert ('ENTRYPOINT ["tini", "--", "python3", "/host/viewer_ctl.py", "hold"]'
            in dockerfile)


def test_viewer_compose_keeps_no_ports_and_host_networking():
    yaml = pytest.importorskip("yaml")
    svc = yaml.safe_load(
        (BROWSER_HOST_DIR / "docker-compose.viewer.yml").read_text())
    svc = svc["services"]["viewer"]
    assert "ports" not in svc
    assert svc["network_mode"] == "host"
    assert svc["user"] == "1000:1000"
    assert svc["restart"] == "no"


# ── 6. SIGTERM / SIGKILL recovery ─────────────────────────────────────

@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
def test_holder_death_frees_the_lock(shared_env, sig):
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; sys.path.insert(0, %r)\n"
         "import manual_mode\n"
         "manual_mode.acquire('owner-1', 'bot-1', root=%r, ttl=6000)\n"
         "time.sleep(30)\n" % (str(BROWSER_HOST_DIR), str(shared_env))],
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            if mm.active("owner-1", "bot-1") is not None:
                break
            time.sleep(0.05)
        assert mm.active("owner-1", "bot-1") is not None, "holder took no lock"
        os.kill(holder.pid, sig)
        holder.wait(timeout=10)
        deadline = time.time() + 10
        while time.time() < deadline:
            if mm.active("owner-1", "bot-1") is None:
                break
            time.sleep(0.05)
        assert mm.active("owner-1", "bot-1") is None, "lock not freed on death"
        # Automation can take it again immediately.
        with mm.acquire("owner-1", "bot-1", kind=mm.KIND_AUTOMATION, ttl=600):
            pass
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


# ── 7. TTL / teardown kills the whole child group ─────────────────────

def test_spawned_children_lead_their_own_process_group():
    proc = vc._spawn([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.3)
        assert os.getpgid(proc.pid) == proc.pid   # start_new_session
    finally:
        vc._terminate_all([proc])
    assert proc.poll() is not None


def test_terminate_all_kills_grandchildren(tmp_path):
    pidfile = tmp_path / "gc.pid"
    code = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', "
        "'import os,time,sys; open(sys.argv[1],\"w\").write(str(os.getpid()));"
        " time.sleep(30)', sys.argv[1]])\n"
        "time.sleep(30)\n"
    )
    proc = vc._spawn([sys.executable, "-c", code, str(pidfile)])
    try:
        gc_pid = None
        deadline = time.time() + 10
        while time.time() < deadline:
            if pidfile.exists() and pidfile.read_text().strip():
                gc_pid = int(pidfile.read_text().strip())
                break
            time.sleep(0.05)
        assert gc_pid, "grandchild never started"

        def alive(pid):
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

        assert alive(gc_pid)
        vc._terminate_all([proc])
        deadline = time.time() + 10
        while time.time() < deadline and alive(gc_pid):
            time.sleep(0.05)
        assert not alive(gc_pid), "grandchild survived _terminate_all"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_hold_acquires_the_lock_before_any_child_starts():
    src = inspect.getsource(vc.hold)
    assert "manual_mode.acquire" in src
    assert "KIND_MANUAL" in src
    assert src.index("manual_mode.acquire") < src.index("build_xvfb_args")


# ── 8. secret hygiene ─────────────────────────────────────────────────

def test_viewer_vnc_pass_is_gitignored():
    assert "browser-host/viewer-vnc-pass" in (REPO_ROOT / ".gitignore").read_text()


def test_vnc_password_file_must_be_mode_0600(tmp_path, monkeypatch):
    monkeypatch.delenv("KYREX_VIEWER_VNC_PASSWORD", raising=False)
    pw = tmp_path / "pass"
    pw.write_text("hunter2\n")
    os.chmod(pw, 0o644)
    monkeypatch.setenv("KYREX_VIEWER_VNC_PASSWORD_FILE", str(pw))
    with pytest.raises(vc.ViewerError):
        vc._vnc_password()
    os.chmod(pw, 0o600)
    assert vc._vnc_password() == "hunter2"


def test_compose_passes_the_password_file_not_the_value():
    text = (BROWSER_HOST_DIR / "docker-compose.viewer.yml").read_text()
    assert "KYREX_VIEWER_VNC_PASSWORD_FILE" in text
    assert "KYREX_VIEWER_VNC_PASSWORD:" not in text   # never the secret value


# ── 9. loopback-only + no CDP + no Cloud egress ───────────────────────

def test_every_listener_is_loopback_and_chromium_has_no_cdp():
    for argv in (vc.build_xvfb_args(), vc.build_x11vnc_args(":99", 5900, "/a"),
                 vc.build_websockify_args("/usr/share/novnc", 6080, 5900),
                 vc.build_chromium_args("/profiles/x")):
        assert not any("0.0.0.0" in a for a in argv)
    assert not any("remote-debugging" in a
                   for a in vc.build_chromium_args("/profiles/x"))


def test_viewer_stack_has_no_cloud_or_railway_viewer_route():
    text = (BROWSER_HOST_DIR / "docker-compose.viewer.yml").read_text().lower()
    assert "railway.app" not in text
    assert "up.railway" not in text
    assert "funnel" not in text
    assert "kyrex_host_cloud_url" not in text
