"""
OpenCode Go session header integration tests.

Verifies that x-opencode-session is injected by the provider/request layer
using a stable per-conversation id owned by the session layer, and that the
header is OpenCode-specific:

  1. OpenCode requests contain x-opencode-session.
  2. The session id is identical across multiple requests in the SAME
     conversation (same TreeSessionManager -> same provider client).
  3. Different conversations receive different session ids.
  4. Other providers (OpenAI, custom OpenAI-compatible, Anthropic) never get
     the OpenCode header.
  5. Existing custom-header behavior is preserved and user headers intact.

The OpenAI SDK is faked at the module boundary (kyrex.providers.openai_.
AsyncOpenAI) so we capture the default_headers that reach the HTTP request
construction without any network call. No production class is subclasses or
modified beyond the existing constructor signature.
"""

import os

import pytest

from kyrex.session.tree import TreeSessionManager
import kyrex.providers.openai_ as openai_module
from kyrex.providers import get_provider


# ── Fake AsyncOpenAI that records the headers it is built with ──────────


class _FakeCompletions:
    def __init__(self, calls):
        self.calls = calls

    async def create(self, **kwargs):
        # Record the full request kwargs (headers are not re-passed per call —
        # they live on the client's default_headers, exactly like the real SDK).
        self.calls.append(kwargs)
        return iter([])  # empty stream: chat() returns an empty result


class _FakeChat:
    def __init__(self, calls):
        self.completions = _FakeCompletions(calls)


class _FakeClient:
    def __init__(self, calls, **client_kwargs):
        self._build_kwargs = client_kwargs
        self.chat = _FakeChat(calls)


@pytest.fixture
def captured_openai(monkeypatch):
    """Replace AsyncOpenAI with a recording fake; returns captured state."""
    state = {"clients": [], "create_calls": []}

    def fake_async_openai(**kwargs):
        client = _FakeClient(state["create_calls"], **kwargs)
        state["clients"].append(client)
        return client

    monkeypatch.setattr(openai_module, "AsyncOpenAI", fake_async_openai)
    yield state


def _client_default_headers(state):
    assert len(state["clients"]) == 1
    return state["clients"][0]._build_kwargs.get("default_headers", {})


# ── 1. OpenCode requests contain x-opencode-session ─────────────────────

class TestOpenCodeHeader:
    def test_opencode_request_has_session_header(self, captured_openai):
        provider = get_provider(
            "openai",
            api_key="sk-test",
            base_url="https://opencode.ai/zen/go/v1",
            session_id="conv-abc-123",
        )
        headers = _client_default_headers(captured_openai)
        assert headers.get("x-opencode-session") == "conv-abc-123"

    def test_all_base_url_variants(self, captured_openai):
        for base in (
            "https://opencode.ai/zen/go/v1",
            "https://opencode.ai",
            "https://opencode.ai/api/v1/chat/completions",
        ):
            captured_openai["clients"].clear()
            get_provider("openai", api_key="k", base_url=base, session_id="s")
            assert _client_default_headers(captured_openai).get("x-opencode-session") == "s"

    def test_provider_named_opencode_gets_header_via_gateway(self, captured_openai):
        # provider "opencode" routes to the OpenAI-compatible provider; the
        # gateway host (not the name) is what triggers the header.
        provider = get_provider(
            "opencode", api_key="k",
            base_url="https://opencode.ai/zen/go/v1", session_id="s1",
        )
        assert _client_default_headers(captured_openai).get("x-opencode-session") == "s1"
        assert provider is not None


# ── 2. Identical session id across requests in the same conversation ────

class TestSessionStability:
    def test_same_tree_manager_returns_same_id(self):
        mgr = TreeSessionManager()
        a = mgr.session_id
        for _ in range(5):
            assert mgr.session_id == a

    def test_same_manager_used_for_multiple_requests(self, captured_openai):
        # One PlaneExecute/session manager -> one provider -> one client. The
        # header is fixed at client construction, so every create() through the
        # same client carries the identical session id (verified via the client
        # default_headers, mirroring the real SDK's per-request behavior).
        from kyrex.providers.openai_ import OpenAIProvider

        client = OpenAIProvider("k", "https://opencode.ai/zen/go/v1", session_id="conv-x")
        # Reuse the same provider for several turns — still ONE client.
        for _ in range(3):
            import asyncio
            asyncio.run(client.chat("gpt-4o", [{"role": "user", "content": "hi"}]))
        headers = _client_default_headers(captured_openai)
        assert headers.get("x-opencode-session") == "conv-x"
        # Every create() went through the same client; no per-request override.
        for call in captured_openai["create_calls"]:
            assert "x-opencode-session" not in call

    def test_load_roundtrip_keeps_id(self, tmp_path):
        mgr = TreeSessionManager(base_path=str(tmp_path))
        mgr.get_session_id("main")
        mgr.save("main")
        sid = mgr._session_ids["main"]

        mgr2 = TreeSessionManager(base_path=str(tmp_path))
        mgr2.load("main")
        assert mgr2.session_id == sid


# ── 3. Different conversations receive different session ids ────────────

class TestDistinctConversations:
    def test_distinct_tree_managers_distinct_ids(self):
        mgr1 = TreeSessionManager()
        mgr2 = TreeSessionManager()
        assert mgr1.session_id != mgr2.session_id

    def test_distinct_branches_distinct_ids(self, tmp_path):
        mgr = TreeSessionManager(base_path=str(tmp_path))
        mgr.get_session_id("main")
        other = mgr.branch("feature-x")
        assert mgr.get_session_id("main") != mgr.get_session_id(other)

    def test_provider_distinct_header_per_session(self, captured_openai):
        get_provider("openai", "k", "https://opencode.ai/zen/go/v1", session_id="conv-a")
        captured_openai["clients"].clear()
        get_provider("openai", "k", "https://opencode.ai/zen/go/v1", session_id="conv-b")
        assert _client_default_headers(captured_openai).get("x-opencode-session") == "conv-b"


# ── 4. Other providers never get the OpenCode header ────────────────────

class TestOpenCodeIsolation:
    @pytest.mark.parametrize("base_url", [
        "https://api.openai.com/v1",
        "https://openrouter.ai/api/v1",
        "http://localhost:11434/v1",
        "https://custom.example.com/v1",
        None,
    ])
    def test_non_opencode_host_no_header(self, captured_openai, base_url):
        get_provider("openai", "k", base_url=base_url, session_id="s")
        assert "x-opencode-session" not in _client_default_headers(captured_openai)

    def test_anthropic_never_gets_header(self, monkeypatch, captured_openai):
        from kyrex.providers import anthropic as anthropic_module

        created = {}

        class _FakeAnthropicClient:
            def __init__(self, **kwargs):
                created["kwargs"] = kwargs

        monkeypatch.setattr(anthropic_module, "AsyncAnthropic", _FakeAnthropicClient)
        provider = get_provider("anthropic", "k", base_url="https://api.anthropic.com", session_id="s")
        assert provider is not None
        # Anthropic client must have zero knowledge of OpenCode headers.
        hdrs = created["kwargs"].get("default_headers") or {}
        assert "x-opencode-session" not in hdrs


# ── 5. Existing custom-header behavior preserved ────────────────────────

class TestCustomHeadersPreserved:
    def test_custom_headers_merged_with_session_header(self, captured_openai):
        custom = {"X-Custom-Key": "v1", "x-api-key": "abc"}
        provider = get_provider(
            "openai", "k", "https://opencode.ai/zen/go/v1",
            extra_headers=custom, session_id="s9",
        )
        headers = _client_default_headers(captured_openai)
        assert headers["X-Custom-Key"] == "v1"
        assert headers["x-api-key"] == "abc"
        assert headers["x-opencode-session"] == "s9"

    def test_custom_headers_untouched_for_non_opencode(self, captured_openai):
        custom = {"X-Custom-Key": "v1"}
        get_provider("openai", "k", "https://api.openai.com/v1", extra_headers=custom, session_id="s")
        headers = _client_default_headers(captured_openai)
        assert headers == custom  # exact dict, no OpenCode header added

    def test_runtime_session_header_replaces_stored_header(self, captured_openai):
        custom = {"x-opencode-session": "user-given-id"}
        provider = get_provider(
            "openai", "k", "https://opencode.ai/zen/go/v1",
            extra_headers=custom, session_id="managed-id",
        )
        assert _client_default_headers(captured_openai).get("x-opencode-session") == "managed-id"

        provider.set_session_id("next-conversation")
        assert len(captured_openai["clients"]) == 2
        headers = captured_openai["clients"][-1]._build_kwargs["default_headers"]
        assert headers["x-opencode-session"] == "next-conversation"

    def test_new_session_rotates_main_identity(self, tmp_path):
        mgr = TreeSessionManager(base_path=str(tmp_path))
        old = mgr.session_id
        branch = mgr.reset_fresh("system", "tree")
        assert mgr.get_session_id(branch) != old
        assert mgr.get_session_id("main") == mgr.get_session_id(branch)

        resumed = TreeSessionManager(base_path=str(tmp_path))
        assert resumed.load("main") is True
        assert resumed.session_id == mgr.get_session_id(branch)

    def test_branch_and_checkout_sync_live_provider(self, tmp_path):
        from kyrex.core import PlaneExecute

        class Provider:
            def __init__(self):
                self.ids = []

            def set_session_id(self, value):
                self.ids.append(value)

        engine = PlaneExecute.__new__(PlaneExecute)
        engine.session = TreeSessionManager(base_path=str(tmp_path))
        engine.provider = Provider()
        main_id = engine.session.session_id

        engine.handle_command("/branch feature")
        feature_id = engine.session.session_id
        assert feature_id != main_id
        assert engine.provider.ids[-1] == feature_id

        engine.handle_command("/checkout main")


class TestSessionHeaderReachBoundary:
    """Trace the header to the actual request-construction layer."""

    def test_header_reaches_create_via_client(self, captured_openai):
        from kyrex.providers.openai_ import OpenAIProvider
        import asyncio

        provider = OpenAIProvider("k", "https://opencode.ai/zen/go/v1", session_id="boundary-check")
        asyncio.run(provider.chat("m", [{"role": "user", "content": "hi"}]))
        # The client was built with the session header on its default_headers
        # (the real SDK applies default_headers to the outbound HTTP request).
        headers = _client_default_headers(captured_openai)
        assert headers.get("x-opencode-session") == "boundary-check"
        assert len(captured_openai["create_calls"]) == 1