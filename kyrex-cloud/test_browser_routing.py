"""Browser Operator routing / wiring tests (serve.py + bots.py integration).

Verifies the host-side half of the phase: the tier table knows every browser
operation, the executor prefix routes, the Bot allowlist round-trips through
the registry and the execution context, and the host-side preflight blocks a
task whose target is not allowlisted.

Pytest-style and import-safe. Run: python3 -m pytest test_browser_routing.py
"""
import json

import pytest

import bots
import serve


def test_browser_operations_are_known():
    for op in (
        "browser:navigate", "browser:read", "browser:click",
        "browser:screenshot", "browser:type", "browser:upload",
        "browser:download", "browser:submit", "browser:delete",
    ):
        assert op in serve.OPERATION_TIERS
        assert op.replace(":", ".", 1) in serve.KNOWN_OPERATIONS


def test_browser_tier_derivation():
    assert serve.derive_host_tier("browser:navigate", "https://example.com") == 0
    assert serve.derive_host_tier("browser:read", "https://example.com") == 0
    assert serve.derive_host_tier("browser:click", "https://example.com") == 0
    assert serve.derive_host_tier("browser:type", "https://example.com") == 1
    assert serve.derive_host_tier("browser:download", "https://example.com") == 1
    assert serve.derive_host_tier("browser:submit", "https://example.com") == 2
    assert serve.derive_host_tier("browser:delete", "https://example.com") == 2


def test_resolve_executor_routes_browser():
    prefix, rest, unknown = serve.resolve_executor(
        'browser: {"actions": [{"action": "read"}]}'
    )
    assert prefix == "browser"
    assert rest == '{"actions": [{"action": "read"}]}'
    assert unknown is None


def test_resolve_executor_unknown_prefix_is_rejected():
    prefix, rest, unknown = serve.resolve_executor("weather: tomorrow")
    assert prefix is None and rest is None
    assert unknown == "weather"


def test_bot_browser_allowlist_normalises_and_drops_junk():
    assert serve.bot_browser_allowlist(
        {"browser_allowlist": ["Example.com", "example.com", "", 7]}
    ) == ["example.com"]
    assert serve.bot_browser_allowlist({}) == []
    assert serve.bot_browser_allowlist({"browser_allowlist": "example.com"}) == []


def test_build_context_carries_owner_and_allowlist(monkeypatch):
    monkeypatch.setattr(bots, "load_bots", lambda: {
        "bid": {
            "id": "bid", "name": "n", "model": "anthropic:x", "rift": "/r",
            "policy": {}, "status": "running", "owner": "alice",
            "browser_allowlist": ["example.com"],
        }
    })
    ctx = serve.build_context("bid")
    assert ctx.bot_id == "bid"
    assert ctx.bot_owner == "alice"
    assert ctx.browser_allowlist == ["example.com"]


def test_unbound_context_has_empty_allowlist(monkeypatch):
    monkeypatch.setattr(bots, "load_bots", lambda: {})
    ctx = serve.build_context("nobody")
    assert ctx.browser_allowlist == []


def test_apply_bot_identity_env_exports_owner_and_allowlist():
    ctx = serve.ExecutionContext(
        session_id="bid", bot_id="bid", bot_owner="alice",
        browser_allowlist=["example.com", "a.test"],
    )
    env = {}
    serve.apply_bot_identity_env(env, ctx)
    assert env["KYREX_BOT_ID"] == "bid"
    assert env["KYREX_BOT_OWNER"] == "alice"
    assert json.loads(env["KYREX_BROWSER_ALLOWLIST"]) == ["example.com", "a.test"]


def test_host_preflight_blocks_unallowlisted_navigation():
    ctx = serve.ExecutionContext(session_id="bid", browser_allowlist=["example.com"])
    blocked = serve.browser_preflight_block(
        ctx, json.dumps({"url": "https://evil.com"})
    )
    assert blocked and "evil.com" in blocked


def test_host_preflight_allows_allowlisted_navigation():
    ctx = serve.ExecutionContext(session_id="bid", browser_allowlist=["example.com"])
    assert serve.browser_preflight_block(
        ctx, json.dumps({"url": "https://example.com/x"})
    ) is None


def test_host_preflight_blocks_unbound_browser_task():
    ctx = serve.build_context("nobody")
    monkeypatched = serve.browser_preflight_block(
        ctx, json.dumps({"url": "https://example.com"})
    )
    assert monkeypatched and "allowlist" in monkeypatched


# ── Registry persistence ───────────────────────────────────────────────

@pytest.fixture
def temp_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    return tmp_path


def test_add_and_update_browser_allowlist(temp_registry):
    bot = bots.add_bot(
        "b1", "Bot One", "anthropic:x", "/rift",
        browser_allowlist=["Example.com", "example.com"],
    )
    assert bot["browser_allowlist"] == ["example.com"]
    updated = bots.update_bot("b1", browser_allowlist=["a.test"])
    assert updated["browser_allowlist"] == ["a.test"]


def test_update_rejects_malformed_allowlist(temp_registry):
    bots.add_bot("b1", "Bot One", "anthropic:x", "/rift")
    with pytest.raises(ValueError):
        bots.update_bot("b1", browser_allowlist=["https://example.com/path"])
    with pytest.raises(ValueError):
        bots.update_bot("b1", browser_allowlist="example.com")
    # The stored value is untouched after a rejected update.
    assert bots.get_bot("b1")["browser_allowlist"] == []


def test_old_registry_backfills_empty_allowlist(temp_registry):
    (temp_registry / "bots.json").write_text(json.dumps({
        "legacy": {
            "id": "legacy", "name": "L", "model": "anthropic:x", "rift": "/r",
            "policy": {}, "status": "stopped", "created_at": "2020-01-01T00:00:00+00:00",
        }
    }))
    loaded = bots.load_bots()
    assert loaded["legacy"]["browser_allowlist"] == []


def test_validate_browser_allowlist_accepts_none():
    assert bots.validate_browser_allowlist(None) == []
