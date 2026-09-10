"""Regression tests: saving a provider profile must not break the
conversations API, and a provider selected on a newly created conversation
must be persisted and used instead of the environment default.

Root cause (regression): provider_profiles.json is written INTO the user's
chat directory as a JSON *array* of encrypted entries. list_conversations()
globbed *.json and called dict.get() on every record, raising
AttributeError -> HTTP 500 on GET /api/conversations / POST /api/conversations.
"""
import asyncio
import json
import os
import sys
import time
from unittest.mock import patch

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-chat-prof-tests")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "env-default-model")
os.environ.setdefault("KYREX_API_KEY", "sk-env-default")
# Fernet key material for provider_profiles (required before import).
os.environ.setdefault("WEB_SESSION_SECRET", "provider-profile-test-secret")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chat_service  # noqa: E402
import provider_profiles  # noqa: E402


def _reset():
    root = chat_service._chat_root()
    for p in root.rglob("*.json"):
        p.unlink()
    for p in root.rglob("*.json.tmp"):
        p.unlink()


def setup_function():
    _reset()


def teardown_function():
    _reset()


def _user_dir(user):
    return provider_profiles._user_dir(user)


def test_saving_provider_profile_does_not_break_conversations_api():
    """Saving provider_profiles.json in the user chat dir must not turn the
    conversation listing into a 500 (AttributeError on a JSON array)."""
    user = "prof-500-user"
    conv = chat_service.create_conversation(user, "Hello")

    # Save a provider profile — writes provider_profiles.json (a LIST) into
    # the same directory that entries from list_conversations() scan.
    profile = provider_profiles.save_profile(user, {
        "id": "openrouter-main",
        "name": "OpenRouter",
        "provider": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "sk-or-test",
        "models": ["openai/gpt-4o-mini"],
    })
    assert profile["id"] == "openrouter-main"
    assert (_user_dir(user) / "provider_profiles.json").exists()

    # Direct contract: list_conversations survives the profile file.
    convs = chat_service.list_conversations(user)
    assert [c["conversation_id"] for c in convs] == [conv["conversation_id"]]
    assert convs[0]["title"] == "Hello"

    # HTTP contract: GET and POST /api/conversations no longer 500.
    import main
    from fastapi.testclient import TestClient

    main.sessions["sess-prof"] = user
    client = TestClient(main.app, cookies={"session": "sess-prof"})

    r = client.get("/api/conversations")
    assert r.status_code == 200, r.text
    assert [c["conversation_id"] for c in r.json()["conversations"]] == \
        [conv["conversation_id"]]

    r2 = client.post("/api/conversations", json={})
    assert r2.status_code == 200, r2.text
    assert set(c["conversation_id"] for c in client.get(
        "/api/conversations").json()["conversations"]) == \
        {conv["conversation_id"], r2.json()["conversation_id"]}


def test_list_conversations_skips_provider_profiles_and_non_dict_json():
    """provider_profiles.json and any other non-dict *.json record must be
    explicitly excluded — never treated as a conversation."""
    user = "prof-skip-user"
    conv = chat_service.create_conversation(user, "Chat")

    provider_profiles.save_profile(user, {
        "id": "p1", "name": "P1", "provider": "openai",
        "base_url": "https://example.com/v1",
        "api_key": "sk-x", "models": ["m1"],
    })
    # A stray non-dict JSON file (e.g. a list) written into the user dir.
    (_user_dir(user) / "stray.json").write_text(json.dumps([1, 2, 3]))

    ids = {c["conversation_id"] for c in chat_service.list_conversations(user)}
    assert ids == {conv["conversation_id"]}


def test_provider_selection_persisted_and_used_for_new_conversation():
    """A provider/model chosen on a NEW conversation is persisted on the
    record and resolved at turn time with the profile's model — not the
    environment default (KYREX_MODEL=env-default-model)."""
    user = "prof-set-user"

    provider_profiles.save_profile(user, {
        "id": "openrouter-alt",
        "name": "OpenRouter Alt",
        "provider": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "sk-or-alt",
        "models": ["anthropic/claude-3.5-sonnet"],
    })

    conv = chat_service.create_conversation(user, "New chat")
    chat_service.set_conversation_provider(
        user, conv["conversation_id"], "openrouter-alt",
        "anthropic/claude-3.5-sonnet")

    # Persisted on the conversation record itself (survives reload).
    stored = chat_service.get_conversation(user, conv["conversation_id"])
    assert stored["provider"] == "openrouter-alt"
    assert stored["model"] == "anthropic/claude-3.5-sonnet"
    assert stored["model"] != os.environ["KYREX_MODEL"]

    # Turn-time resolution: stream_chat resolves the PER-CONVERSATION values.
    calls = []

    class RecordingProvider:
        async def chat(self, model, messages, tools=None, stream_callback=None,
                       interrupt_event=None, **kw):
            calls.append({"model": model})
            if stream_callback:
                stream_callback("ok")
            return {"role": "assistant", "content": "ok"}

    original_resolve = chat_service._resolve_provider

    def recording_resolve(provider_id=None, selected_model=None, user=None):
        calls.append({"provider_id": provider_id, "selected_model": selected_model})
        return original_resolve(provider_id, selected_model, user=user)

    async def run():
        gen = chat_service.stream_chat(user, conv["conversation_id"], "hi")
        async for f in gen:
            if f.get("type") == "status" and f.get("status") == "complete":
                break

    with patch("chat_service.get_provider", return_value=RecordingProvider()), \
         patch.object(chat_service, "_resolve_provider", side_effect=recording_resolve):
        asyncio.run(run())

    assert calls[0] == {"provider_id": "openrouter-alt",
                        "selected_model": "anthropic/claude-3.5-sonnet"}, calls
    # The profile's model reached the provider call — never the env default.
    assert calls[-1]["model"] == "anthropic/claude-3.5-sonnet", calls
    assert not any(c.get("model") == "env-default-model" for c in calls)
    assert chat_service._resolve_provider("openrouter-alt",
                                          "anthropic/claude-3.5-sonnet",
                                          user=user)["base_url"] == \
        "https://openrouter.ai/api/v1"
    # Cleanup: the recorded engine binding must not leak across tests.
    chat_service.close_engine_session(user, conv["conversation_id"])
    time.sleep(0)
