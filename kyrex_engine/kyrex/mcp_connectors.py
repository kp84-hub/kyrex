"""Offline curated MCP connector configuration and installation support.

This module constructs structured MCP command/argument configurations and
persists them only through MCPManager. It deliberately does not start
processes, perform network access, or store credentials.
"""

from dataclasses import dataclass
import os
import shutil
from typing import Iterable


class ConnectorConfigurationError(ValueError):
    """Raised when a connector cannot be safely configured."""


class ConnectorInstallationError(RuntimeError):
    """Raised when a validated connector cannot be persisted."""


@dataclass(frozen=True)
class ConnectorConfig:
    """The command/args/environment configuration persisted by MCPManager."""

    name: str
    command: str
    args: tuple[str, ...]
    env: dict[str, str]


class ConnectorConfigurator:
    """Reusable catalog connector configurator.

    Catalog entries are data only. Authentication is validated against the
    environment and never copied into ConnectorConfig or mcp_servers.json.
    """

    def __init__(self, connector: dict):
        self.connector = connector

    @property
    def connector_id(self) -> str:
        return self.connector["id"]

    def validate_prerequisites(self) -> None:
        command = self.connector.get("command", "")
        if not command:
            raise ConnectorConfigurationError(
                f"{self.connector['name']}: official command is not representable yet"
            )
        if shutil.which(command) is None:
            raise ConnectorConfigurationError(
                f"{self.connector['name']}: prerequisite executable '{command}' was not found"
            )
        if self.connector.get("verification", {}).get("status") != "verified":
            raise ConnectorConfigurationError(
                f"{self.connector['name']}: connector configuration is not verified"
            )

        auth = self.connector.get("auth", {})
        for variable in auth.get("required_environment", []):
            if not os.environ.get(variable, "").strip():
                raise ConnectorConfigurationError(
                    f"{self.connector['name']}: missing configuration {variable}"
                )

    def configuration(self) -> ConnectorConfig:
        self.validate_prerequisites()
        args = self.connector.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) and arg for arg in args):
            raise ConnectorConfigurationError(
                f"{self.connector['name']}: invalid command arguments"
            )
        if any("<" in arg or ">" in arg for arg in args):
            raise ConnectorConfigurationError(
                f"{self.connector['name']}: required interactive configuration is not set"
            )
        env = self.connector.get("env", {})
        if not isinstance(env, dict) or not all(
            isinstance(key, str) and key and isinstance(value, str) and value
            for key, value in env.items()
        ):
            raise ConnectorConfigurationError(
                f"{self.connector['name']}: invalid environment configuration"
            )
        return ConnectorConfig(
            name=self.connector_id,
            command=self.connector["command"],
            args=tuple(args),
            env=dict(env),
        )

    def install(self, manager) -> ConnectorConfig:
        config = self.configuration()
        try:
            if config.env:
                manager.add(config.name, config.command, list(config.args), config.env)
            else:
                manager.add(config.name, config.command, list(config.args))
        except Exception as exc:
            raise ConnectorInstallationError(
                f"{self.connector['name']}: installation failed: {exc}"
            ) from exc
        return config


def configure_connector(connector: dict, manager) -> ConnectorConfig:
    """Validate, configure, and persist one catalog connector."""
    return ConnectorConfigurator(connector).install(manager)


def connector_by_id(connectors: Iterable[dict], connector_id: str) -> dict | None:
    """Return a catalog entry by stable ID without external lookups."""
    return next((item for item in connectors if item.get("id") == connector_id), None)
