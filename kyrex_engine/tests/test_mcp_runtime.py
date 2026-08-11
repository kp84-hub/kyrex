import json

import json
import sys

from kyrex.tools.mcp import MCPManager, MCPServer


class FakeServer(MCPServer):
    def __init__(self, name, result=None, error=""):
        super().__init__(name, "fake")
        self.result = result
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
        self.tools = self.result or []
        self._disabled = False
        self._error = ""
        self.process = object()


def test_mcp_server_start_creates_nested_env_path_parent(tmp_path):
    nested_file = tmp_path / "missing" / "deep" / "memory.jsonl"
    server = MCPServer(
        "memory",
        sys.executable,
        ["-c", "import sys; [print('{\"result\":{\"tools\":[]}}', flush=True) for _ in sys.stdin]"],
        {"MEMORY_FILE_PATH": str(nested_file)},
    )

    server.start()
    try:
        assert nested_file.parent.is_dir()
        assert server.process is not None
    finally:
        server.stop()


def test_manager_test_connection_uses_existing_server_and_reports_tools(tmp_path):
    manager = MCPManager()
    manager.config_path = tmp_path / "mcp_servers.json"
    manager.config_path.write_text(json.dumps({"browser": {"command": "fake", "args": []}}))
    server = FakeServer("browser", result=[{"name": "open"}, {"name": "click"}])
    manager.servers["browser"] = server

    result = manager.test_connection("browser")

    assert result == {
        "success": True,
        "server": "browser",
        "tool_count": 2,
        "error": "",
    }
    assert server.stopped is True


def test_manager_test_connection_reports_missing_server(tmp_path):
    manager = MCPManager()
    manager.config_path = tmp_path / "mcp_servers.json"

    result = manager.test_connection("missing")

    assert result["success"] is False
    assert result["tool_count"] == 0
    assert "not configured" in result["error"]


def test_manager_test_connection_reports_authentication_failure(tmp_path):
    manager = MCPManager()
    manager.config_path = tmp_path / "mcp_servers.json"
    manager.config_path.write_text(json.dumps({"github": {"command": "fake", "args": []}}))
    manager.servers["github"] = FakeServer(
        "github", error="Initialization error: missing authentication token"
    )

    result = manager.test_connection("github")

    assert result["success"] is False
    assert "missing authentication token" in result["error"]


def test_manager_test_connection_reports_startup_failure(tmp_path):
    manager = MCPManager()
    manager.config_path = tmp_path / "mcp_servers.json"
    manager.servers["browser"] = MCPServer("browser", "/definitely/not/a/command")

    result = manager.test_connection("browser")

    assert result["success"] is False
    assert result["tool_count"] == 0
    assert "Failed to start" in result["error"]
