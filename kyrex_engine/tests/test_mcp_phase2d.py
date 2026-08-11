import json

import json

import pytest

from kyrex.tools.mcp import MCPManager, MCPServer


class FakeServer(MCPServer):
    def __init__(self, name, result=None, error=""):
        super().__init__(name, "fake")
        self.result = result or []
        self.expected_error = error
        self.stopped = False

    def stop(self):
        self.stopped = True
        self.process = None

    def start(self):
        if self.expected_error:
            self._disabled = True
            self._error = self.expected_error
            return
        self.tools = self.result
        self._disabled = False
        self._error = ""
        self.process = object()


def test_full_connector_lifecycle_refresh_test_and_remove(tmp_path):
    config_path = tmp_path / "mcp_servers.json"
    manager = MCPManager()
    manager.config_path = config_path

    manager.add("browser", "fake", ["--stdio"])
    assert json.loads(config_path.read_text()) == {
        "browser": {"command": "fake", "args": ["--stdio"]}
    }

    manager.servers["browser"] = FakeServer(
        "browser", result=[{"name": "open"}, {"name": "click"}]
    )
    result = manager.test_connection("browser")
    assert result["success"] is True
    assert result["tool_count"] == 2

    config_path.write_text(json.dumps({
        "browser": {"command": "fake", "args": ["--stdio"]},
        "docs": {"command": "fake", "args": []},
    }))
    manager.refresh()
    assert set(manager.servers) == {"browser", "docs"}

    manager.remove("browser")
    assert json.loads(config_path.read_text()) == {
        "docs": {"command": "fake", "args": []}
    }
    manager.refresh()
    assert set(manager.servers) == {"docs"}


def test_failed_add_restores_memory_and_preserves_file(tmp_path, monkeypatch):
    config_path = tmp_path / "mcp_servers.json"
    manager = MCPManager()
    manager.config_path = config_path
    manager.add("existing", "fake", [])
    original = config_path.read_bytes()
    previous = manager.servers["existing"]

    def fail_save():
        raise OSError("simulated persistence failure")

    monkeypatch.setattr(manager, "_save_config", fail_save)
    with pytest.raises(OSError, match="simulated persistence failure"):
        manager.add("new", "fake", [])

    assert set(manager.servers) == {"existing"}
    assert manager.servers["existing"] is previous
    assert config_path.read_bytes() == original


def test_failed_remove_restores_memory_and_preserves_file(tmp_path, monkeypatch):
    manager = MCPManager()
    manager.config_path = tmp_path / "mcp_servers.json"
    manager.add("existing", "fake", [])
    original = manager.config_path.read_bytes()
    existing = manager.servers["existing"]

    monkeypatch.setattr(manager, "_save_config", lambda: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        manager.remove("existing")

    assert manager.servers["existing"] is existing
    assert manager.config_path.read_bytes() == original


def test_failed_atomic_replace_leaves_previous_config_intact(tmp_path, monkeypatch):
    manager = MCPManager()
    manager.config_path = tmp_path / "mcp_servers.json"
    manager.add("existing", "fake", [])
    original = manager.config_path.read_bytes()

    real_replace = __import__("os").replace
    monkeypatch.setattr("kyrex.tools.mcp.os.replace", lambda *_: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError, match="replace failed"):
        manager.add("new", "fake", [])

    assert manager.config_path.read_bytes() == original
    assert "new" not in manager.servers
    assert not list(manager.config_path.parent.glob(".*mcp_servers.json.*"))
    monkeypatch.setattr("kyrex.tools.mcp.os.replace", real_replace)


def test_refresh_reconciles_removed_and_changed_entries(tmp_path):
    manager = MCPManager()
    manager.config_path = tmp_path / "mcp_servers.json"
    manager.add("old", "fake", [])
    manager.config_path.write_text(json.dumps({
        "new": {"command": "other", "args": ["x"]},
    }))

    old_server = manager.servers["old"]
    manager.refresh()

    assert set(manager.servers) == {"new"}
    assert manager.servers["new"].command == "other"
    assert old_server.process is None


def test_refresh_rejects_corrupt_config_without_mutating_memory(tmp_path):
    manager = MCPManager()
    manager.config_path = tmp_path / "mcp_servers.json"
    manager.add("healthy", "fake", [])
    existing = manager.servers["healthy"]
    manager.config_path.write_text("{not valid json")

    with pytest.raises(json.JSONDecodeError):
        manager.refresh()

    assert manager.servers["healthy"] is existing
