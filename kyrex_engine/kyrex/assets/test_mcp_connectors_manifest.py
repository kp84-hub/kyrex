import json
from pathlib import Path


MANIFEST = Path(__file__).with_name("mcp-connectors.json")


def test_mcp_connector_manifest_schema():
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert isinstance(document, dict)
    assert document["schema_version"] == 1
    connectors = document["connectors"]
    assert isinstance(connectors, list)
    assert len(connectors) == 8
    assert {connector["id"] for connector in connectors} == {
        "filesystem",
        "cloudflare",
        "everything",
        "fetch",
        "git",
        "memory",
        "sequential-thinking",
        "time",
    }

    for connector in connectors:
        assert set(connector) == {
            "id",
            "name",
            "description",
            "command",
            "args",
            "requirements",
            "auth",
            "source_url",
            "verification",
        }
        assert all(isinstance(connector[key], str) and connector[key] for key in (
            "id", "name", "description", "command", "source_url"
        ))
        assert isinstance(connector["args"], list)
        assert all(isinstance(argument, str) for argument in connector["args"])
        assert isinstance(connector["requirements"], list)
        assert all(isinstance(requirement, str) for requirement in connector["requirements"])

        auth = connector["auth"]
        assert set(auth) == {"mode", "warning"}
        assert auth["mode"] in {"none", "api_key", "environment_variable", "browser_sign_in", "manual_setup"}
        assert isinstance(auth["warning"], str) and auth["warning"]

        verification = connector["verification"]
        assert set(verification) == {"status", "checked_at"}
        assert verification["status"] == "verified"
        assert isinstance(verification["checked_at"], str) and verification["checked_at"]
