from kyrex.core import _run_tool_with_timeout


def test_mcp_call_tool_dispatch_uses_named_arguments():
    calls = []

    def call_tool(full_name, arguments):
        calls.append((full_name, arguments))
        return {"ok": True}

    result_holder = {}
    _run_tool_with_timeout(
        call_tool,
        "mcp_browser_open",
        {"full_name": "mcp_browser_open", "arguments": {"url": "https://example.test"}},
        result_holder,
    )

    assert result_holder == {"result": {"ok": True}}
    assert calls == [("mcp_browser_open", {"url": "https://example.test"})]
