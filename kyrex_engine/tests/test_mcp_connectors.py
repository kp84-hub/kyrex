import json
import json

import pytest

from kyrex.core import _load_mcp_connector_manifest
from kyrex.mcp_connectors import (
    ConnectorConfigurationError,
    ConnectorInstallationError,
    ConnectorConfigurator,
    configure_connector,
)
from kyrex.tools.mcp import MCPManager


class FakeManager:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def add(self, name, command, args):
        if self.fail:
            raise OSError("offline persistence failure")
        self.calls.append((name, command, args))


def connector(connector_id):
    return next(item for item in _load_mcp_connector_manifest() if item["id"] == connector_id)


def test_connector_configuration_generation(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/" + command)
    config = ConnectorConfigurator(connector("playwright")).configuration()
    assert config.name == "playwright"
    assert config.command == "npx"
    assert config.args == ("-y", "@playwright/mcp@latest")


def test_connector_specific_requirements_and_missing_configuration(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/" + command)
    github = ConnectorConfigurator(connector("github"))
    with pytest.raises(ConnectorConfigurationError, match="not representable"):
        github.configuration()

    context7 = connector("context7")
    context7["auth"]["required_environment"] = ["CONTEXT7_API_KEY"]
    with pytest.raises(ConnectorConfigurationError, match="CONTEXT7_API_KEY"):
        ConnectorConfigurator(context7).configuration()


def test_prerequisite_validation_and_invalid_configuration(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda command: None)
    with pytest.raises(ConnectorConfigurationError, match="not found"):
        ConnectorConfigurator(connector("playwright")).configuration()

    invalid = dict(connector("playwright"))
    invalid["args"] = ["<required-value>"]
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/" + command)
    with pytest.raises(ConnectorConfigurationError, match="interactive configuration"):
        ConnectorConfigurator(invalid).configuration()


def test_sentry_unfilled_access_token_placeholder_is_rejected(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/" + command)

    with pytest.raises(ConnectorConfigurationError, match="interactive configuration"):
        ConnectorConfigurator(connector("sentry")).configuration()


def test_installation_uses_manager_and_never_persists_credentials(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/" + command)
    manager = FakeManager()
    configure_connector(connector("context7"), manager)
    assert manager.calls == [("context7", "npx", ["-y", "@upstash/context7-mcp"])]
    assert "CONTEXT7_API_KEY" not in json.dumps(manager.calls)


def test_installation_failure_is_reported(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/" + command)
    with pytest.raises(ConnectorInstallationError, match="installation failed"):
        configure_connector(connector("playwright"), FakeManager(fail=True))


def test_mcp_manager_persistence_add_and_remove(tmp_path):
    manager = MCPManager()
    manager.config_path = tmp_path / "mcp_servers.json"
    manager.add("playwright", "npx", ["-y", "@playwright/mcp@latest"])
    assert json.loads(manager.config_path.read_text()) == {
        "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}
    }
    manager.remove("playwright")
    assert json.loads(manager.config_path.read_text()) == {}


def test_installed_state_refresh_comes_from_manager(tmp_path):
    manager = MCPManager()
    manager.config_path = tmp_path / "mcp_servers.json"
    manager.add("context7", "npx", ["-y", "@upstash/context7-mcp"])
    reloaded = MCPManager()
    reloaded.config_path = manager.config_path
    reloaded._load_config()
    assert set(reloaded.servers) == {"context7"}
    assert "playwright" not in reloaded.servers
