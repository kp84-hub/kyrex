from kyrex.core import _load_mcp_connector_manifest


def test_mcp_connector_manifest_schema():
    connectors = _load_mcp_connector_manifest()

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
