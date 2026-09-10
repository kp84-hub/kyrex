"""
Setup wizard OpenCode session header tests.

Proves the `kx --setup` behavior for the OpenCode provider:

  1. Selecting OpenCode auto-generates a random UUID for x-opencode-session.
  2. Re-running setup generates a DIFFERENT UUID and replaces the previous one.
  3. The generated header is persisted in the saved config file AND reaches
     the request-construction layer (the same config-header precedence the
     provider test suite already covers).
  4. Non-OpenCode providers keep existing custom headers exactly as before;
     no OpenCode UUID is injected, and nothing about their flow changes.
  5. The wizard never prints or prefills the session UUID (no exposure).
  6. An explicit user-entered x-opencode-session still wins.

The wizard is driven with scripted input()/getpass() answers; network calls
(fetching models, connection test) are stubbed so the tests are hermetic.
"""

import json
import re
import uuid
from pathlib import Path

import pytest

from kyrex.config import ConfigManager, _generate_opencode_session_id

# OpenCode gateway base URL as produced by the wizard preset for OpenCode.
OPENCODE_URL = "https://opencode.ai/zen/go/v1"
OPENAI_URL = "https://api.openai.com/v1"

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class _ScriptedInput:
    """Feeds scripted answers to input()/getpass() calls."""

    def __init__(self, answers):
        self.answers = list(answers)

    def __call__(self, prompt=""):
        if not self.answers:
            raise AssertionError(
                f"wizard asked for more input than scripted (leftover prompt: {prompt!r})"
            )
        return self.answers.pop(0)


@pytest.fixture
def wizard_env(monkeypatch, clean_env):
    """Stub every blocking/network path the wizard touches."""
    monkeypatch.setattr(ConfigManager, "_fetch_model_list", lambda self, *a, **k: None)
    monkeypatch.setattr(ConfigManager, "test_connection", lambda self: (True, "ok"))
    return monkeypatch


def _run_wizard(cfg_path, input_answers, getpass_answers, monkeypatch):
    """Run setup_wizard once against cfg_path with scripted answers.

    Mirrors the real `kx --setup` entry path (core_bridge.py): the manager
    loads the existing config first so prefills / header preservation work.
    """
    cm = ConfigManager(Path(cfg_path))
    cm.load()
    monkeypatch.setattr("builtins.input", _ScriptedInput(input_answers))
    monkeypatch.setattr("getpass.getpass", _ScriptedInput(getpass_answers))
    cm.setup_wizard()
    return cm


def _load_saved(cfg_path):
    cfg = json.loads(Path(cfg_path).read_text())
    return cfg


# ── 1. OpenCode setup generates a UUID ──────────────────────────────────

class TestOpenCodeSetupGeneratesUuid:
    def test_opencode_wizard_stores_session_uuid(self, tmp_path, wizard_env, monkeypatch):
        cfg_path = tmp_path / "config.json"
        _run_wizard(
            cfg_path,
            input_answers=["1", "gpt-4o", "y"],      # OpenCode preset, model, save
            getpass_answers=["sk-test", ""],         # api key, headers (blank)
            monkeypatch=monkeypatch,
        )
        saved = _load_saved(cfg_path)
        assert saved["base_url"] == OPENCODE_URL
        headers = saved.get("headers", {})
        assert "x-opencode-session" in headers
        assert UUID_RE.fullmatch(headers["x-opencode-session"])

    def test_generated_id_is_a_fresh_random_uuid(self):
        a = _generate_opencode_session_id()
        b = _generate_opencode_session_id()
        assert UUID_RE.fullmatch(a)
        assert UUID_RE.fullmatch(b)
        assert a != b
        # Never the RFC-4122 example id / the value from the earlier manual test.
        assert a != "550e8400-e29b-41d4-a716-446655440000"


# ── 2. Re-running setup produces a different UUID ───────────────────────

class TestSetupReplacement:
    def test_rerun_replaces_previous_uuid(self, tmp_path, wizard_env, monkeypatch):
        cfg_path = tmp_path / "config.json"
        _run_wizard(
            cfg_path,
            input_answers=["1", "gpt-4o", "y"],
            getpass_answers=["sk-test", ""],
            monkeypatch=monkeypatch,
        )
        first = _load_saved(cfg_path)["headers"]["x-opencode-session"]
        assert UUID_RE.fullmatch(first)

        # Second run against the same file: same flow, different UUID.
        cm2 = ConfigManager(Path(cfg_path))
        cm2.load()
        _run_wizard(
            cfg_path,
            input_answers=["1", "gpt-4o", "y"],
            getpass_answers=["sk-test", ""],
            monkeypatch=monkeypatch,
        )
        second = _load_saved(cfg_path)["headers"]["x-opencode-session"]
        assert UUID_RE.fullmatch(second)
        assert second != first

    def test_rerun_replaces_even_when_previous_header_preserved_base(self, tmp_path, wizard_env, monkeypatch):
        # Same as above but the previous config already had a stored session
        # header; the wizard must still rotate it on every run.
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({
            "provider": "openai",
            "model": "gpt-4o",
            "base_url": OPENCODE_URL,
            "api_key": "sk-test",
            "headers": {"x-opencode-session": "550e8400-e29b-41d4-a716-446655440000"},
        }))
        _run_wizard(
            cfg_path,
            input_answers=["1", "gpt-4o", "y"],
            getpass_answers=["sk-test", ""],
            monkeypatch=monkeypatch,
        )
        new_id = _load_saved(cfg_path)["headers"]["x-opencode-session"]
        assert new_id != "550e8400-e29b-41d4-a716-446655440000"
        assert UUID_RE.fullmatch(new_id)


# ── 3. Persistence: saved config + request-layer delivery ───────────────

class TestPersistence:
    def test_header_persisted_and_reaches_request_client(self, tmp_path, wizard_env, monkeypatch):
        cfg_path = tmp_path / "config.json"
        _run_wizard(
            cfg_path,
            input_answers=["1", "gpt-4o", "y"],
            getpass_answers=["sk-test", ""],
            monkeypatch=monkeypatch,
        )
        saved = _load_saved(cfg_path)
        session_uuid = saved["headers"]["x-opencode-session"]

        # Reload through ConfigManager -> get_headers() feeds extra_headers
        # into get_provider -> the AsyncOpenAI client's default_headers.
        cm = ConfigManager(Path(cfg_path))
        cm.load()
        headers = cm.get_headers()
        assert headers["x-opencode-session"] == session_uuid

        # The stored header serves setup connection testing; the live runtime
        # conversation id must replace it at request construction.
        from kyrex.providers import get_provider

        built = {}

        class _FakeCompletions:
            async def create(self, **kwargs):
                return iter([])

        class _FakeChat:
            def __init__(self):
                self.completions = _FakeCompletions()

        class _FakeClient:
            def __init__(self, **client_kwargs):
                built["kwargs"] = client_kwargs
                self.chat = _FakeChat()

        import kyrex.providers.openai_ as openai_module

        monkeypatch.setattr(openai_module, "AsyncOpenAI", _FakeClient)
        get_provider(
            "openai", "sk-test", base_url=OPENCODE_URL,
            extra_headers=headers, session_id="per-conv-id",
        )
        client_headers = built["kwargs"].get("default_headers", {})
        assert client_headers["x-opencode-session"] == "per-conv-id"

    def test_connection_test_sees_generated_header(self, tmp_path, wizard_env, monkeypatch):
        # The wizard's own connection test reads get_headers() — the generated
        # header must be present during the test request too.
        cfg_path = tmp_path / "config.json"
        seen = {}

        def fake_test(self):
            seen["headers"] = self.get_headers()
            return True, "ok"

        monkeypatch.setattr(ConfigManager, "test_connection", fake_test)
        _run_wizard(
            cfg_path,
            input_answers=["1", "gpt-4o", "y"],
            getpass_answers=["sk-test", ""],
            monkeypatch=monkeypatch,
        )
        assert UUID_RE.fullmatch(seen["headers"]["x-opencode-session"])


# ── 4. Non-OpenCode custom headers unaffected ───────────────────────────

class TestNonOpenCodeUnaffected:
    def test_openai_setup_injects_no_session_header(self, tmp_path, wizard_env, monkeypatch):
        cfg_path = tmp_path / "config.json"
        _run_wizard(
            cfg_path,
            input_answers=["3", "gpt-4o", "y"],      # OpenAI preset
            getpass_answers=["sk-2", ""],
            monkeypatch=monkeypatch,
        )
        saved = _load_saved(cfg_path)
        assert saved["base_url"] == OPENAI_URL
        assert "headers" not in saved  # nothing was entered, nothing injected

    def test_existing_non_opencode_headers_preserved(self, tmp_path, wizard_env, monkeypatch):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({
            "provider": "openai",
            "model": "gpt-4o",
            "base_url": OPENAI_URL,
            "api_key": "sk-2",
            "headers": {"X-Tenant": "acme", "X-Region": "eu"},
        }))
        _run_wizard(
            cfg_path,
            input_answers=["3", "gpt-4o", "y"],
            getpass_answers=["sk-2", ""],            # blank -> keep existing
            monkeypatch=monkeypatch,
        )
        saved = _load_saved(cfg_path)
        assert saved["headers"] == {"X-Tenant": "acme", "X-Region": "eu"}
        assert "x-opencode-session" not in saved["headers"]

    def test_stale_session_header_preserved_when_switching_away(self, tmp_path, wizard_env, monkeypatch):
        # Config previously used OpenCode (has a stored session UUID) and the
        # user now selects OpenAI: existing custom headers are left exactly as
        # they were — the wizard never silently rewrites non-OpenCode configs.
        # (The stale OpenCode token is inert on non-OpenCode hosts — the
        # provider layer only sends it to the opencode.ai gateway.)
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({
            "provider": "openai",
            "model": "gpt-4o",
            "base_url": OPENCODE_URL,
            "api_key": "sk-2",
            "headers": {"X-Tenant": "acme", "x-opencode-session": "stale-uuid-1"},
        }))
        _run_wizard(
            cfg_path,
            input_answers=["3", "gpt-4o", "y"],
            getpass_answers=["sk-2", ""],
            monkeypatch=monkeypatch,
        )
        saved = _load_saved(cfg_path)
        assert saved["base_url"] == OPENAI_URL
        assert saved["headers"] == {"X-Tenant": "acme", "x-opencode-session": "stale-uuid-1"}

    def test_opencode_rerun_preserves_custom_headers_rotates_session(self, tmp_path, wizard_env, monkeypatch):
        # OpenCode re-run: unrelated custom headers survive the session rotation.
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({
            "provider": "openai",
            "model": "gpt-4o",
            "base_url": OPENCODE_URL,
            "api_key": "sk-test",
            "headers": {"X-Tenant": "acme", "x-opencode-session": "old-uuid"},
        }))
        _run_wizard(
            cfg_path,
            input_answers=["1", "gpt-4o", "y"],
            getpass_answers=["sk-test", ""],
            monkeypatch=monkeypatch,
        )
        saved = _load_saved(cfg_path)
        assert saved["headers"]["X-Tenant"] == "acme"
        new_id = saved["headers"]["x-opencode-session"]
        assert new_id != "old-uuid"
        assert UUID_RE.fullmatch(new_id)

    def test_typed_custom_headers_for_openai_stored_verbatim(self, tmp_path, wizard_env, monkeypatch):
        cfg_path = tmp_path / "config.json"
        _run_wizard(
            cfg_path,
            input_answers=["3", "gpt-4o", "y"],
            getpass_answers=["sk-2", "X-Tenant=acme"],
            monkeypatch=monkeypatch,
        )
        saved = _load_saved(cfg_path)
        assert saved["headers"] == {"X-Tenant": "acme"}


# ── 5. No UUID exposure in wizard output ────────────────────────────────

class TestNoExposure:
    def test_uuid_not_printed_in_wizard_output(self, tmp_path, wizard_env, monkeypatch, capsys):
        cfg_path = tmp_path / "config.json"
        _run_wizard(
            cfg_path,
            input_answers=["1", "gpt-4o", "y"],
            getpass_answers=["sk-test", ""],
            monkeypatch=monkeypatch,
        )
        session_uuid = _load_saved(cfg_path)["headers"]["x-opencode-session"]
        out = capsys.readouterr().out
        assert session_uuid not in out
        # The summary masks it instead of printing the raw token.
        assert "x-opencode-session=<auto>" in out


# ── 6. Explicit user value wins ─────────────────────────────────────────

class TestExplicitUserValueWins:
    def test_user_typed_session_header_kept(self, tmp_path, wizard_env, monkeypatch):
        cfg_path = tmp_path / "config.json"
        _run_wizard(
            cfg_path,
            input_answers=["1", "gpt-4o", "y"],
            getpass_answers=["sk-test", "x-opencode-session=user-given-id"],
            monkeypatch=monkeypatch,
        )
        saved = _load_saved(cfg_path)
        assert saved["headers"]["x-opencode-session"] == "user-given-id"


# ── Shared detection used by both layers ────────────────────────────────

class TestSharedDetection:
    def test_config_and_provider_use_same_detector(self):
        from kyrex.config import is_opencode_gateway as cfg_detect
        from kyrex.providers.openai_ import _is_opencode_gateway as provider_detect

        for url in (OPENCODE_URL, "https://opencode.ai", "https://api.opencode.ai/v1"):
            assert cfg_detect(url) is True
            assert provider_detect(url) is True
        for url in (OPENAI_URL, "https://openrouter.ai/api/v1",
                    "http://localhost:11434/v1", "https://custom.example.com/v1", None):
            assert cfg_detect(url) is False
            assert provider_detect(url) is False