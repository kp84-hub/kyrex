#!/usr/bin/env python3
"""Tests for rift_cleanup.py — guarded cleanup of runtime Bot Rifts.

Runs two ways:

  * ``python3 -m pytest kyrex-cloud/test_rift_cleanup.py`` (pytest-style:
    the module is import-safe, so conftest collects it), and
  * ``python3 kyrex-cloud/test_rift_cleanup.py`` (standalone runner).

Every test is sandboxed: ``KYREX_DATA_DIR`` points at a pytest ``tmp_path``,
and ``bots.BOTS_FILE`` / ``audit.AUDIT_FILE`` are redirected there too. No
test ever touches the real ``~/.kyrex`` or deletes user data.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

import audit
import bots
import paths
import rift_cleanup as rc


# ── Helpers ────────────────────────────────────────────────────────────

def _sandbox(tmp, monkeypatch):
    """Redirect every persisted path into *tmp*; return the Rift root."""
    monkeypatch.setenv("KYREX_DATA_DIR", str(tmp))
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp / "bots.json"))
    monkeypatch.setattr(audit, "AUDIT_FILE", str(tmp / "audit.jsonl"))
    root = tmp / "rifts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _run_git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True,
                   capture_output=True, text=True,
                   env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def _git_clean_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _run_git(path, "init", "-q")
    (path / "readme.txt").write_text("hello\n")
    _run_git(path, "add", "-A")
    _run_git(path, "-c", "user.email=t@e.x", "-c", "user.name=t",
             "commit", "-qm", "init")
    return path


def _age(path, days):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def _add_bot(bot_id, rift_path, owner="", status="stopped"):
    return bots.add_bot(bot_id, "N", "m", str(rift_path), owner=owner,
                        status=status)


def _cleanup(bot_id, **kw):
    return rc.cleanup_bot_rift(bot_id, **kw)


# ── Protected Rifts ────────────────────────────────────────────────────

def test_persistent_rift_is_never_removed(tmp_path, monkeypatch):
    root = _sandbox(tmp_path, monkeypatch)
    rift = root / "devbot"
    rift.mkdir()
    (rift / rc.PERSISTENT_MARKER).write_text("devbot\n")
    _age(rift, 30)
    _add_bot("devbot", rift, owner="alice")

    # Even an operator cannot delete a protected Rift.
    res = _cleanup("devbot", operator=True)
    assert res["removed"] is False
    assert "persistent" in res["reasons"]
    assert rift.exists(), "protected Rift must survive cleanup"

    res2 = _cleanup("devbot", operator=True, dry_run=False,
                    retention_seconds=0)
    assert res2["removed"] is False
    assert rift.exists()


# ── Dirty Rifts ────────────────────────────────────────────────────────

def test_dirty_rift_refused(tmp_path, monkeypatch):
    root = _sandbox(tmp_path, monkeypatch)
    rift = _git_clean_repo(root / "dirtybot")
    (rift / "readme.txt").write_text("changed but not committed\n")
    _age(rift, 30)
    _add_bot("dirtybot", rift, owner="alice")

    res = _cleanup("dirtybot", owner="alice")
    assert res["removed"] is False
    assert "dirty" in res["reasons"]
    assert rift.exists()


def test_non_git_nonempty_rift_refused(tmp_path, monkeypatch):
    root = _sandbox(tmp_path, monkeypatch)
    rift = root / "plainbot"
    rift.mkdir()
    (rift / "data.txt").write_text("unverifiable\n")
    _age(rift, 30)
    _add_bot("plainbot", rift, owner="alice")

    res = _cleanup("plainbot", owner="alice")
    assert res["removed"] is False
    assert "unverifiable_non_git" in res["reasons"]
    assert rift.exists()


# ── Active tasks / lease ───────────────────────────────────────────────

def test_active_task_refused(tmp_path, monkeypatch):
    root = _sandbox(tmp_path, monkeypatch)
    rift = _git_clean_repo(root / "busybot")
    _age(rift, 30)
    _add_bot("busybot", rift, owner="alice")

    import task_store
    store = task_store.CloudTaskStore(tmp_path / "cloud_tasks.db")
    tid = store.submit(session_key="busybot", task_text="do work",
                       bot_id="busybot", rift=str(rift), resolve_bot=False)
    store.set_status(tid, "running")
    store.close()

    res = _cleanup("busybot", owner="alice")
    assert res["removed"] is False
    assert "active_tasks" in res["reasons"]
    assert rift.exists()


def test_active_lease_refused(tmp_path, monkeypatch):
    root = _sandbox(tmp_path, monkeypatch)
    rift = _git_clean_repo(root / "leasedbot")
    _age(rift, 30)
    _add_bot("leasedbot", rift, owner="alice")
    (rift / rc.LEASE_FILE).write_text(
        json.dumps({"holder": "worker-1", "pid": os.getpid(),
                    "heartbeat_at": time.time()}))

    res = _cleanup("leasedbot", owner="alice")
    assert res["removed"] is False
    assert "active_lease" in res["reasons"]
    assert rift.exists()


# ── Retention window ───────────────────────────────────────────────────

def test_retention_window_blocks_then_allows(tmp_path, monkeypatch):
    root = _sandbox(tmp_path, monkeypatch)
    rift = root / "freshbot"
    rift.mkdir()  # empty -> not dirty, not unverifiable
    _age(rift, 1)  # 1 day old
    _add_bot("freshbot", rift, owner="alice")

    res = _cleanup("freshbot", owner="alice")  # default 7-day window
    assert res["removed"] is False
    assert "within_retention" in res["reasons"]
    assert rift.exists()

    # Beyond the window it becomes eligible and is removed.
    _age(rift, 30)
    res2 = _cleanup("freshbot", owner="alice")
    assert res2["removed"] is True
    assert not rift.exists()


# ── Owner authorization ────────────────────────────────────────────────

def test_owner_authorization(tmp_path, monkeypatch):
    root = _sandbox(tmp_path, monkeypatch)
    rift = root / "ownedbot"
    rift.mkdir()
    _age(rift, 30)
    _add_bot("ownedbot", rift, owner="alice")

    wrong = _cleanup("ownedbot", owner="bob")
    assert wrong["removed"] is False
    assert "owner_mismatch" in wrong["reasons"]
    assert rift.exists()

    none = _cleanup("ownedbot")
    assert none["removed"] is False
    assert "not_authorized" in none["reasons"]
    assert rift.exists()

    ok = _cleanup("ownedbot", operator=True)
    assert ok["removed"] is True
    assert not rift.exists()


def test_owner_match_deletes(tmp_path, monkeypatch):
    root = _sandbox(tmp_path, monkeypatch)
    rift = root / "ownok"
    rift.mkdir()
    _age(rift, 30)
    _add_bot("ownok", rift, owner="alice")

    res = _cleanup("ownok", owner="alice")
    assert res["removed"] is True
    assert not rift.exists()


# ── Containment ────────────────────────────────────────────────────────

def test_outside_rifts_root_refused(tmp_path, monkeypatch):
    root = _sandbox(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("do not delete\n")
    _age(outside, 30)
    _add_bot("escapebot", outside, owner="alice")

    with pytest.raises(rc.RiftRefused) as exc:
        _cleanup("escapebot", operator=True)
    assert exc.value.reason == "outside_rifts_root"
    assert outside.exists() and (outside / "keep.txt").exists()


def test_rift_root_itself_refused(tmp_path, monkeypatch):
    root = _sandbox(tmp_path, monkeypatch)
    _add_bot("rootbot", root, owner="alice")
    with pytest.raises(rc.RiftRefused) as exc:
        _cleanup("rootbot", operator=True)
    assert exc.value.reason == "rift_is_root"
    assert root.exists()


def test_traversal_path_refused(tmp_path, monkeypatch):
    root = _sandbox(tmp_path, monkeypatch)
    evil = tmp_path / "escape"
    evil.mkdir()
    _add_bot("trapbot", root / ".." / "escape", owner="alice")
    with pytest.raises(rc.RiftRefused) as exc:
        _cleanup("trapbot", operator=True)
    assert exc.value.reason == "outside_rifts_root"
    assert evil.exists()


# ── Successful cleanup + audit + dry-run/list-stale ─────────────────────

def test_successful_cleanup_records_audit(tmp_path, monkeypatch):
    root = _sandbox(tmp_path, monkeypatch)
    rift = _git_clean_repo(root / "goodbot")
    _age(rift, 30)
    _add_bot("goodbot", rift, owner="alice")

    res = _cleanup("goodbot", owner="alice")
    assert res["removed"] is True and res["decision"] == "allow"
    assert not rift.exists()

    entries = audit.read_entries(bot_id="goodbot")
    assert any(e["operation"] == "rift_cleanup" and e["decision"] == "allow"
               for e in entries)


def test_dry_run_deletes_nothing(tmp_path, monkeypatch):
    root = _sandbox(tmp_path, monkeypatch)
    rift = _git_clean_repo(root / "drybot")
    _age(rift, 30)
    _add_bot("drybot", rift, owner="alice")

    res = _cleanup("drybot", owner="alice", dry_run=True)
    assert res["removed"] is False and res["eligible"] is True
    assert res["dry_run"] is True
    assert rift.exists()

    entries = audit.read_entries(bot_id="drybot")
    assert any(e["operation"] == "rift_cleanup" and e["outcome"] == "dry_run"
               for e in entries)


def test_list_stale_reports_without_mutation(tmp_path, monkeypatch):
    root = _sandbox(tmp_path, monkeypatch)
    eligible = root / "oldbot"
    eligible.mkdir()
    _age(eligible, 30)
    fresh = root / "newbot"
    fresh.mkdir()

    rows = {r["bot_id"]: r for r in rc.list_stale_rifts()}
    assert rows["oldbot"]["eligible"] is True
    assert rows["newbot"]["eligible"] is False
    assert "within_retention" in rows["newbot"]["reasons"]
    # A listing never deletes.
    assert eligible.exists() and fresh.exists()


def test_unknown_bot_raises_keyerror(tmp_path, monkeypatch):
    _sandbox(tmp_path, monkeypatch)
    with pytest.raises(KeyError):
        _cleanup("ghost", operator=True)


def test_blob_refused(tmp_path, monkeypatch):
    root = _sandbox(tmp_path, monkeypatch)
    _add_bot("blobbot", str(root), owner="alice")
    with pytest.raises(rc.RiftRefused):
        _cleanup("blobbot", operator=True)


# ── Stopping a Bot never deletes its Rift ──────────────────────────────

def test_stopped_bot_keeps_its_rift(tmp_path, monkeypatch):
    root = _sandbox(tmp_path, monkeypatch)
    rift = _git_clean_repo(root / "statbot")
    _age(rift, 30)
    _add_bot("statbot", rift, owner="alice", status="running")

    bots.set_status("statbot", "stopped")
    assert rift.exists(), "a stopped Bot must keep its Rift"
    assert not audit.read_entries(bot_id="statbot")


# ── Standalone runner ──────────────────────────────────────────────────

class _FakeMonkey:
    """Minimal monkeypatch stand-in for the standalone runner."""

    def __init__(self):
        self._saved = []

    def setenv(self, name, value):
        self._saved.append(("env", name, os.environ.get(name)))
        os.environ[name] = value

    def setattr(self, obj, name, value):
        self._saved.append(("attr", obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for rec in reversed(self._saved):
            if rec[0] == "env":
                _, name, old = rec
                if old is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old
            else:
                _, obj, name, old = rec
                setattr(obj, name, old)
        self._saved.clear()


if __name__ == "__main__":
    import tempfile
    import traceback

    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        mp = _FakeMonkey()
        try:
            with tempfile.TemporaryDirectory() as td:
                params = fn.__code__.co_varnames[:fn.__code__.co_argcount]
                if "monkeypatch" in params:
                    fn(Path(td), mp)
                else:
                    fn(Path(td), mp)
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        finally:
            mp.undo()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
