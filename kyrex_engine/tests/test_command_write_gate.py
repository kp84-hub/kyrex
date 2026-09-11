"""Engine-side command-write gate tests.

A run_command that changes the Rift clone must surface those paths through the
same protocol-backed confirmation gate the edit/deletion gates use, BEFORE the
tool result is returned and the model's turn continues.

The TUI resolves the gate from `confirm_response` (via
`toolbox._confirmation_results` + `_pending_confirmations`), exactly as
core_bridge.stdin_thread does. These tests drive the REAL gate protocol, so the
package-level conftest autouse fixture (which stubs the gates) is overridden.
"""

import io
import json
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

import kyrex.toolbox as toolbox
from kyrex.toolbox import ToolBox


@pytest.fixture(autouse=True)
def auto_approve_gates():
    """Override conftest: exercise the real command-write gate protocol."""
    yield


class GateResponder:
    """Wraps sys.stdout, captures protocol frames, resolves gates.

    Mirrors core_bridge.stdin_thread: a confirm_request is resolved by writing
    the decision into _confirmation_results and signalling the pending Event.
    """

    def __init__(self, approve=True, approve_deletion=True):
        self.buffer = io.StringIO()
        self.messages = []
        self.approve = approve
        self.approve_deletion = approve_deletion

    def write(self, text):
        self.buffer.write(text)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            self.messages.append(msg)
            if msg.get("type") == "confirm_request":
                cid = msg.get("id")
                if cid is None:
                    continue
                if msg.get("value") == "deletion":
                    approved = self.approve_deletion
                elif msg.get("value") == "command_write":
                    approved = self.approve
                else:
                    approved = True
                toolbox._confirmation_results[cid] = approved
                event = toolbox._pending_confirmations.get(cid)
                if event is not None:
                    event.set()
        return len(text)

    def flush(self):
        pass

    def find(self, mtype):
        return [m for m in self.messages if m.get("type") == mtype]

    def command_writes(self):
        return [m for m in self.messages if m.get("value") == "command_write"]


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", repo, *args], check=True, capture_output=True, text=True
    )


@pytest.fixture
def clone(tmp_path, monkeypatch):
    """A git clone root with a committed baseline file."""
    repo = tmp_path / "clone"
    repo.mkdir()
    (repo / "base.txt").write_text("base\n")
    _git(str(repo), "init", "-q")
    _git(str(repo), "add", "-A")
    _git(str(repo), "-c", "user.email=t@example.com",
         "-c", "user.name=t", "commit", "-qm", "init")

    source = tmp_path / "source"
    source.mkdir()

    monkeypatch.chdir(repo)
    monkeypatch.setenv("WORKSPACE_ROOT", str(repo))
    monkeypatch.setenv("PROJECT_SOURCE_ROOT", str(source))
    monkeypatch.setattr("kyrex.toolbox._is_interactive", lambda: True)
    # Deterministic: never use a real bwrap binary.
    monkeypatch.setattr("kyrex.toolbox.shutil.which", lambda _name: None)
    return repo


def _tool():
    return ToolBox(MagicMock())


def test_command_write_emits_live_gate_and_approves(clone, monkeypatch):
    responder = GateResponder(approve=True)
    monkeypatch.setattr(sys, "stdout", responder)

    result = _tool().run_command("touch created.txt")

    assert result.get("status") == "ok"
    writes = responder.command_writes()
    assert writes, "a command that changed the clone must emit a command_write gate"
    assert writes[0]["value"] == "command_write"
    assert any(p.endswith("created.txt") for p in writes[0]["paths"])
    assert (clone / "created.txt").exists()


def test_command_write_gate_carries_display_and_paths(clone, monkeypatch):
    responder = GateResponder(approve=True)
    monkeypatch.setattr(sys, "stdout", responder)

    _tool().run_command("touch created.txt")

    payload = responder.command_writes()[0]
    # Display text is display-only; the real paths ride in "paths".
    assert payload["path"] == "1 file(s) changed by command"
    assert payload["paths"] and payload["paths"][0].endswith("created.txt")
    assert "outside the normal diff gate" in payload["diff"]


def test_command_write_denial_returns_error(clone, monkeypatch):
    responder = GateResponder(approve=False)
    monkeypatch.setattr(sys, "stdout", responder)

    result = _tool().run_command("touch created.txt")

    assert "error" in result
    assert "discarded" in result["error"].lower()
    writes = responder.command_writes()
    assert writes and any(p.endswith("created.txt") for p in writes[0]["paths"])


def test_no_change_no_gate(clone, monkeypatch):
    responder = GateResponder(approve=True)
    monkeypatch.setattr(sys, "stdout", responder)

    result = _tool().run_command("true")

    assert result.get("status") == "ok"
    assert not responder.command_writes()


def test_preexisting_dirty_file_excluded_from_baseline(clone, monkeypatch):
    # The operator's own uncommitted work existed before the first command.
    (clone / "dirty.txt").write_text("operator\n")
    responder = GateResponder(approve=True)
    monkeypatch.setattr(sys, "stdout", responder)

    _tool().run_command("touch new.txt")

    writes = responder.command_writes()
    assert writes, "the new file must still be gated"
    for w in writes:
        assert not any(p.endswith("dirty.txt") for p in w["paths"])
    assert any(p.endswith("new.txt") for p in writes[0]["paths"])


def test_command_modifying_preexisting_dirty_file_is_not_reported(clone, monkeypatch):
    (clone / "dirty.txt").write_text("operator\n")
    responder = GateResponder(approve=True)
    monkeypatch.setattr(sys, "stdout", responder)

    result = _tool().run_command("echo more >> dirty.txt")

    assert result.get("status") == "ok"
    # The only change was to a pre-existing dirty file: never reported.
    assert not responder.command_writes()


def test_deletion_command_not_double_gated(clone, monkeypatch):
    victim = clone / "base.txt"
    responder = GateResponder(approve=True, approve_deletion=True)
    monkeypatch.setattr(sys, "stdout", responder)

    result = _tool().run_command(f"rm {victim}")

    assert result.get("status") == "ok"
    assert not victim.exists()
    # The deletion gate owns this change; the command-write gate must not fire.
    assert not responder.command_writes()
    assert any(m.get("value") == "deletion" for m in responder.find("confirm_request"))


def test_non_git_workspace_skips_gate(tmp_path, monkeypatch):
    """A non-git workspace cannot be diffed: the gate is skipped, not guessed."""
    plain = tmp_path / "plain"
    plain.mkdir()
    source = tmp_path / "source2"
    source.mkdir()
    monkeypatch.chdir(plain)
    monkeypatch.setenv("WORKSPACE_ROOT", str(plain))
    monkeypatch.setenv("PROJECT_SOURCE_ROOT", str(source))
    responder = GateResponder(approve=True)
    monkeypatch.setattr(sys, "stdout", responder)

    result = _tool().run_command("touch created.txt")

    assert result.get("status") == "ok"
    assert not responder.command_writes()


class SilentResponder(GateResponder):
    """Captures protocol frames but NEVER resolves a gate.

    Models a frontend that received the confirm_request and never answered, so
    the engine's own wait must time out and deny.
    """

    def write(self, text):
        self.buffer.write(text)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if isinstance(msg, dict):
                self.messages.append(msg)
        return len(text)


def test_command_write_timeout_denies_and_reverts(clone, monkeypatch):
    """A gate that is never answered must time out into a hard DENY: the tool
    path does not continue, and only the command-introduced clone changes are
    reverted (clone left clean)."""
    monkeypatch.setattr(toolbox, "_COMMAND_WRITE_TIMEOUT", 0.2)
    responder = SilentResponder(approve=True)
    monkeypatch.setattr(sys, "stdout", responder)

    result = _tool().run_command("touch created.txt")

    # The tool path did NOT continue as a success — it received an error.
    assert "error" in result
    assert result.get("status") != "ok"
    assert "discarded" in result["error"].lower()
    assert "timed out" in result["error"].lower()
    # The gate was emitted, but never resolved into a live approval.
    assert responder.command_writes(), "the gate must still be emitted"
    assert not any(toolbox._confirmation_results.values())
    # Only the command-introduced change was reverted: the clone is clean.
    assert not (clone / "created.txt").exists()


def test_command_write_timeout_leaves_preexisting_dirty(clone, monkeypatch):
    """Timeout reverts only command-introduced paths: a pre-existing dirty file
    is left untouched."""
    (clone / "dirty.txt").write_text("operator\n")
    monkeypatch.setattr(toolbox, "_COMMAND_WRITE_TIMEOUT", 0.2)
    responder = SilentResponder(approve=True)
    monkeypatch.setattr(sys, "stdout", responder)

    result = _tool().run_command("touch created.txt")

    assert "error" in result
    assert not (clone / "created.txt").exists()
    assert (clone / "dirty.txt").read_text() == "operator\n"
