"""Security remediation tests.

Covers the fixes from the Kyrex security review that follow the read-only
enforcement work:

  - credential/config files are written owner-only (0600) at rest
  - the data root is created owner-only (0700)
  - fs_executor re-verifies path containment after the approval round-trip
    (the approval wait is a TOCTOU window)
  - fs_executor caps read content so one huge file cannot exhaust memory
  - OAuth login state is validated (single-use, expiring) in the web backend
"""
import importlib
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit  # noqa: E402
import fs_executor  # noqa: E402
import paths  # noqa: E402
import serve  # noqa: E402


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _last_result_json(capsys) -> dict:
    captured = capsys.readouterr()
    lines = [
        line for line in captured.out.splitlines()
        if line.startswith("KYREX_RESULT_JSON:")
    ]
    assert lines, f"no result JSON in output: {captured.out!r}"
    return json.loads(lines[-1][len("KYREX_RESULT_JSON:"):])


# ── credentials at rest ──────────────────────────────────────────────

def test_data_dir_created_owner_only(tmp_path, monkeypatch):
    fresh = tmp_path / "data" / "root"
    monkeypatch.setenv("KYREX_DATA_DIR", str(fresh))

    resolved = paths.data_dir()

    assert resolved == fresh.resolve()
    assert fresh.exists()
    assert _mode(fresh) == 0o700


def test_write_mcp_config_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_SERVERS_JSON", json.dumps({"server": {"command": "uvx"}}))
    serve._MCP_WRITTEN = False
    serve.MCP_SERVERS_DIR = tmp_path
    serve.MCP_SERVERS_FILE = tmp_path / "mcp_servers.json"
    try:
        serve.write_mcp_config()

        assert serve.MCP_SERVERS_FILE.exists()
        assert _mode(serve.MCP_SERVERS_FILE) == 0o600
    finally:
        serve._MCP_WRITTEN = False


def test_write_mcp_config_tightens_preexisting_loose_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_SERVERS_JSON", json.dumps({"server": {"command": "uvx"}}))
    serve._MCP_WRITTEN = False
    serve.MCP_SERVERS_DIR = tmp_path
    serve.MCP_SERVERS_FILE = tmp_path / "mcp_servers.json"
    serve.MCP_SERVERS_FILE.write_text("{}")
    os.chmod(serve.MCP_SERVERS_FILE, 0o644)
    try:
        serve.write_mcp_config()
        assert _mode(serve.MCP_SERVERS_FILE) == 0o600
    finally:
        serve._MCP_WRITTEN = False


def test_audit_log_is_owner_only(tmp_path, monkeypatch):
    audit_file = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit, "AUDIT_FILE", str(audit_file))

    audit.log("bot-1", "fs.read", "tier0", "allow", "auto")

    assert audit_file.exists()
    assert _mode(audit_file) == 0o600


# ── fs_executor: post-approval TOCTOU re-verification ────────────────

def test_fs_read_rejects_symlink_swap_during_approval(tmp_path, monkeypatch, capsys):
    root = tmp_path / "fsroot"
    root.mkdir()
    victim = root / "victim.txt"
    victim.write_text("inside content")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret")

    def swap_during_approval(emit_fn):
        victim.unlink()
        victim.symlink_to(outside)
        return True

    monkeypatch.setattr(fs_executor, "_get_operation_verdict", swap_during_approval)

    fs_executor._handle_read(["read", "victim.txt"], root)

    result = _last_result_json(capsys)
    assert result["status"] == "error"
    assert any(
        "escaped the filesystem root after approval" in (e or "")
        for e in result["errors"]
    )
    assert "outside secret" not in result["final_response"]


def test_fs_write_rejects_symlink_swap_during_approval(tmp_path, monkeypatch, capsys):
    root = tmp_path / "fsroot"
    root.mkdir()
    target = root / "w.txt"
    target.write_text("original")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret")

    def swap_during_approval(emit_fn):
        target.unlink()
        target.symlink_to(outside)
        return True

    monkeypatch.setattr(fs_executor, "_get_operation_verdict", swap_during_approval)

    fs_executor._handle_write(["write", "w.txt <<< clobber"], root)

    result = _last_result_json(capsys)
    assert result["status"] == "error"
    assert any(
        "escaped the filesystem root after approval" in (e or "")
        for e in result["errors"]
    )
    # The write must not have landed outside the root.
    assert outside.read_text() == "outside secret"


def test_fs_delete_rejects_symlink_swap_during_approval(tmp_path, monkeypatch, capsys):
    root = tmp_path / "fsroot"
    root.mkdir()
    target = root / "d.txt"
    target.write_text("to delete")

    def swap_during_approval(emit_fn):
        # Resolve-then-remove race: the file is swapped before the verdict.
        target.unlink()
        target.mkdir()
        return True

    monkeypatch.setattr(fs_executor, "_get_operation_verdict", swap_during_approval)

    fs_executor._handle_delete(["delete", "d.txt"], root)

    result = _last_result_json(capsys)
    assert result["status"] == "error"
    # The swapped-out path must not have been removed: it is a directory now.
    assert target.is_dir()


def test_fs_read_containment_still_enforced_without_swap(tmp_path, monkeypatch, capsys):
    root = tmp_path / "fsroot"
    root.mkdir()
    (root / "ok.txt").write_text("hello")

    monkeypatch.setattr(fs_executor, "_get_operation_verdict", lambda emit_fn: True)

    fs_executor._handle_read(["read", "ok.txt"], root)

    result = _last_result_json(capsys)
    assert result["status"] == "ok"
    assert result["final_response"] == "hello"


# ── fs_executor: read cap ────────────────────────────────────────────

def test_fs_read_content_is_capped(tmp_path, monkeypatch, capsys):
    root = tmp_path / "fsroot"
    root.mkdir()
    big = root / "big.txt"
    big.write_text("x" * (fs_executor.MAX_READ_BYTES + 10_000))

    monkeypatch.setattr(fs_executor, "_get_operation_verdict", lambda emit_fn: True)

    fs_executor._handle_read(["read", "big.txt"], root)

    result = _last_result_json(capsys)
    assert result["status"] == "ok"
    assert "[truncated at" in result["final_response"]
    assert len(result["final_response"]) <= fs_executor.MAX_READ_BYTES + 200


# ── web backend: OAuth state ─────────────────────────────────────────

def _load_web_main(tmp_path, monkeypatch):
    """Load web/backend/main.py with the env it requires at import."""
    backend = (
        Path(__file__).resolve().parent / "web" / "backend" / "main.py"
    )
    monkeypatch.setenv("GITHUB_CLIENT_ID", "test-client")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("WEB_ALLOWED_GITHUB_USERNAME", "tester")
    monkeypatch.setenv("KYREX_DATA_DIR", str(tmp_path / "data"))
    spec = importlib.util.spec_from_file_location("web_main", backend)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError:
        pytest.skip("web backend dependencies (fastapi/uvicorn) not installed")
    return module


def test_oauth_state_single_use_and_expiring(tmp_path, monkeypatch):
    web_main = _load_web_main(tmp_path, monkeypatch)

    # Unknown state fails closed.
    assert not web_main._consume_oauth_state("never-issued")
    assert not web_main._consume_oauth_state("")
    assert not web_main._consume_oauth_state(None)

    # A freshly issued state is accepted exactly once.
    state = web_main._issue_oauth_state()
    assert web_main._consume_oauth_state(state)
    assert not web_main._consume_oauth_state(state)

    # An expired state fails closed even though it is still in the table.
    state = web_main._issue_oauth_state()
    web_main._oauth_states[state] = (
        time.monotonic() - web_main.OAUTH_STATE_TTL_SECONDS - 1
    )
    assert not web_main._consume_oauth_state(state)
