import copy

import pytest

from kyrex.core import _load_mcp_connector_manifest, _validate_mcp_connector_manifest


EXPECTED_IDS = {
    "filesystem",
    "cloudflare",
    "everything",
    "fetch",
    "git",
    "memory",
    "sequential-thinking",
    "time",
    "github",
    "playwright",
    "context7",
    "notion",
    "sentry",
}


def test_mcp_connector_manifest_schema():
    connectors = _load_mcp_connector_manifest()

    assert len(connectors) == 13
    assert len(connectors) == len(EXPECTED_IDS)
    assert {connector["id"] for connector in connectors} == EXPECTED_IDS
    for connector in connectors:
        assert connector["category"]
        assert connector["installation_notes"]
        assert connector["prerequisites"]
        assert isinstance(connector["command"], str)
        assert isinstance(connector["args"], list)
        assert "required_environment" in connector["auth"]


def test_mcp_connector_manifest_categories():
    connectors = _load_mcp_connector_manifest()
    categories = {connector["category"] for connector in connectors}

    assert categories == {"Cloud", "Development", "Utilities", "Web"}


def test_mcp_connector_manifest_rejects_malformed_entries():
    connectors = _load_mcp_connector_manifest()
    malformed = copy.deepcopy(connectors[0])
    malformed.pop("category")

    with pytest.raises(ValueError, match="invalid connector"):
        _validate_mcp_connector_manifest({"schema_version": 1, "connectors": [malformed]})


def test_mcp_connector_manifest_rejects_invalid_prerequisites():
    connectors = _load_mcp_connector_manifest()
    malformed = copy.deepcopy(connectors[0])
    malformed["prerequisites"] = [""]

    with pytest.raises(ValueError, match="prerequisites"):
        _validate_mcp_connector_manifest({"schema_version": 1, "connectors": [malformed]})
