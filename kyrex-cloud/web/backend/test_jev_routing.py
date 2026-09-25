"""Tests for Jev's bounded active Chat routing layer."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
_ENGINE = _REPO / "kyrex_engine"
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_ENGINE))

import jev_routing  # noqa: E402
import jev_stream_router  # noqa: E402


class FakeClient:
    def __init__(self, *, route="engine", confidence=0.95,
                 risk="low", review=0.1):
        self.route = route
        self.confidence = confidence
        self.risk = risk
        self.review = review
        self.calls = []

    def decide(self, state, questions):
        self.calls.append((state, questions))
        return {
            "model": "jev-test",
            "answers": {
                "route": {
                    "type": "choice",
                    "choice": self.route,
                    "probabilities": {self.route: 1.0},
                    "confidence": self.confidence,
                },
                "risk": {
                    "type": "choice",
                    "choice": self.risk,
                    "probabilities": {self.risk: 1.0},
                    "confidence": 0.99,
                },
                "needs_review": {
                    "type": "noul",
                    "noul": self.review,
                },
            },
            "usage": {},
        }


def test_disabled_uses_deterministic_route(tmp_path, monkeypatch):
    monkeypatch.setenv("KYREX_JEV_ROUTING_LOG", str(tmp_path / "route.jsonl"))
    result = jev_routing.decide_route(
        "explain this code", {"engine", "repo"}, "repo", enabled=False)
    assert result["selected_route"] == "repo"
    assert result["source"] == "deterministic"
    assert result["reason"] == "jev_disabled"


def test_active_jev_can_deescalate_repo_to_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("KYREX_JEV_ROUTING_LOG", str(tmp_path / "route.jsonl"))
    client = FakeClient(route="engine", risk="low", review=0.05)
    result = jev_routing.decide_route(
        "what does this function do?", {"engine", "repo"}, "repo",
        client=client, enabled=True)
    assert result["selected_route"] == "engine"
    assert result["source"] == "jev"
    assert result["risk"] == "low"
    assert client.calls[0][0]["available_routes"] == ["engine", "repo"]


def test_illegal_route_never_widens_authority(tmp_path, monkeypatch):
    monkeypatch.setenv("KYREX_JEV_ROUTING_LOG", str(tmp_path / "route.jsonl"))
    client = FakeClient(route="calendar_delete")
    result = jev_routing.decide_route(
        "delete something", {"engine", "browser"}, "browser",
        client=client, enabled=True)
    assert result["selected_route"] == "browser"
    assert result["source"] == "deterministic"
    assert result["reason"] == "illegal_route"


def test_review_blocks_privilege_increase(tmp_path, monkeypatch):
    monkeypatch.setenv("KYREX_JEV_ROUTING_LOG", str(tmp_path / "route.jsonl"))
    client = FakeClient(route="repo", risk="high", review=0.95)
    result = jev_routing.decide_route(
        "change the repo", {"engine", "repo"}, "engine",
        client=client, enabled=True)
    assert result["selected_route"] == "engine"
    assert result["reason"] == "review_blocks_privilege_increase"


def test_review_does_not_block_safer_deescalation(tmp_path, monkeypatch):
    monkeypatch.setenv("KYREX_JEV_ROUTING_LOG", str(tmp_path / "route.jsonl"))
    client = FakeClient(route="engine", risk="high", review=0.95)
    result = jev_routing.decide_route(
        "explain before acting", {"engine", "repo"}, "repo",
        client=client, enabled=True)
    assert result["selected_route"] == "engine"
    assert result["source"] == "jev"


def test_routing_log_never_persists_request_text(tmp_path, monkeypatch):
    log = tmp_path / "route.jsonl"
    monkeypatch.setenv("KYREX_JEV_ROUTING_LOG", str(log))
    secret = "SUPER-SECRET-REQUEST-CONTENT"
    jev_routing.decide_route(
        secret, {"engine", "repo"}, "repo",
        client=FakeClient(route="engine"), enabled=True)
    stored = log.read_text(encoding="utf-8")
    assert secret not in stored
    record = json.loads(stored)
    assert record["jev_route"] == "engine"


def _fake_exact_developer_modules():
    preset = {"fs:read": 0, "repo:read": 0, "fs:write": 1, "repo:pr": 1}
    serve = SimpleNamespace(
        DEVELOPER_PRESET=preset,
        is_browser_bot_policy=lambda policy: False,
    )
    dev_bot = SimpleNamespace()
    dev_bot.is_writable_bot_policy = lambda policy: policy == preset
    dev_bot.browser_route_ready = lambda bot: False
    # The active shim also wraps owner-scoped connected-tool readiness. These
    # older execution-route tests do not exercise those paths, so their minimal
    # doubles explicitly report both unavailable (fail closed).
    dev_bot.gmail_route_ready = lambda bot: False
    dev_bot.email_calendar_route_ready = lambda bot: False
    return preset, serve, dev_bot


def test_stream_shim_only_deescalates_during_route_selection(monkeypatch):
    # Fresh install state for this isolated fake-module test.
    monkeypatch.setattr(jev_stream_router, "_installed", False)

    preset, serve, dev_bot = _fake_exact_developer_modules()
    calls = []

    async def original_stream(user, conversation_id, user_content,
                              cancel_event=None, workspace_id=None,
                              request_id=None):
        calls.append(dev_bot.is_writable_bot_policy(preset))
        yield {"type": "conversation", "conversation_id": conversation_id}
        # The shim must clear its ContextVar before the caller consumes work
        # beyond route selection.
        calls.append(dev_bot.is_writable_bot_policy(preset))
        yield {"type": "status", "status": "complete", "content": "ok"}

    chat_service = SimpleNamespace(
        stream_chat=original_stream,
        get_conversation=lambda user, cid: {"bot_id": "dev"},
        resolve_bot_for_user=lambda user, bid: {
            "id": "dev", "policy": dict(preset)},
        serve=serve,
    )

    monkeypatch.setattr(
        jev_stream_router.jev_routing, "decide_route",
        lambda *a, **k: {"selected_route": "engine"})
    jev_stream_router.install(chat_service, dev_bot)

    async def consume():
        return [frame async for frame in chat_service.stream_chat(
            "alice", "c1", "explain the code")]

    frames = asyncio.run(consume())
    assert frames[-1]["status"] == "complete"
    assert calls == [False, True]


def test_custom_mixed_policy_is_never_deescalated(monkeypatch):
    monkeypatch.setattr(jev_stream_router, "_installed", False)

    preset, serve, dev_bot = _fake_exact_developer_modules()
    mixed = dict(preset)
    mixed["cal:list"] = 0
    dev_bot.is_writable_bot_policy = lambda policy: bool(policy.get("fs:write"))
    calls = []

    async def original_stream(user, conversation_id, user_content,
                              cancel_event=None, workspace_id=None,
                              request_id=None):
        calls.append(dev_bot.is_writable_bot_policy(mixed))
        yield {"type": "status", "status": "complete", "content": "ok"}

    chat_service = SimpleNamespace(
        stream_chat=original_stream,
        get_conversation=lambda user, cid: {"bot_id": "custom"},
        resolve_bot_for_user=lambda user, bid: {
            "id": "custom", "policy": mixed},
        serve=serve,
    )

    monkeypatch.setattr(
        jev_stream_router.jev_routing, "decide_route",
        lambda *a, **k: {"selected_route": "engine"})
    jev_stream_router.install(chat_service, dev_bot)

    async def consume():
        return [frame async for frame in chat_service.stream_chat(
            "alice", "c2", "explain")]

    asyncio.run(consume())
    assert calls == [True]
