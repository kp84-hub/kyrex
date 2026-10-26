"""Concurrency regressions for the Chat routing boundary; no live inference."""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parents[1], _HERE.parents[2] / "kyrex_engine"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import jev_routing  # noqa: E402
import jev_stream_router  # noqa: E402


@pytest.fixture
def chat(monkeypatch, tmp_path):
    monkeypatch.setattr(jev_stream_router, "_installed", False)
    monkeypatch.setenv("KYREX_JEV_ROUTING_LOG", str(tmp_path / "jev.jsonl"))
    monkeypatch.setitem(sys.modules, "fitness_delegation", SimpleNamespace(
        is_read_request=lambda _: False))
    monkeypatch.setitem(sys.modules, "vision", SimpleNamespace(
        eligible=lambda _: False, followup_images=lambda *_: False))
    monkeypatch.setitem(sys.modules, "chat_maps", SimpleNamespace(
        requested=lambda _: False, bot_allowed=lambda *_: False))
    observed = []
    sessions = {}
    preset = {"fs:write": 1}

    def resolve(user, bid):
        return {"id": bid, "owner": user, "policy": {"bot:delegate": 0}}

    async def stream(user, cid, text, **kwargs):
        session = service._get_engine_session(user)
        observed.append({
            "user": user,
            "hint": session._jev_bot_target_hint,
            "context": jev_stream_router._bot_target_hint.get(),
        })
        yield {"type": "status", "status": "complete", "user": user}

    service = SimpleNamespace(
        stream_chat=stream,
        get_conversation=lambda user, cid: {"bot_id": user + "-chief"},
        resolve_bot_for_user=resolve,
        _get_engine_session=lambda user: sessions.setdefault(user, SimpleNamespace()),
        serve=SimpleNamespace(
            DEVELOPER_PRESET=preset,
            is_browser_bot_policy=lambda _: False,
            coordinator_granted=lambda bot: "bot:delegate" in bot["policy"],
        ),
    )
    dev = SimpleNamespace(
        is_writable_bot_policy=lambda policy: policy == preset,
        browser_route_ready=lambda _: False,
        gmail_route_ready=lambda _: False,
        email_calendar_route_ready=lambda _: False,
    )
    monkeypatch.setattr(jev_stream_router, "_routing_candidates",
                        lambda _service, user, bot: [
                            {"id": bot["id"], "name": "Coordinator"},
                            {"id": user + "-mail", "name": "Mail"},
                        ])
    monkeypatch.setattr(jev_stream_router, "_shared_tools",
                        lambda *_: ["gmail_read"])
    jev_stream_router.install(service, dev)
    return service, observed, sessions, preset


async def consume(service, user="alice", **kwargs):
    return [frame async for frame in service.stream_chat(
        user, user + "-conversation", user + " request", **kwargs)]


def test_independent_route_and_target_work_overlap_once(chat, monkeypatch):
    service, observed, _, _ = chat
    rendezvous = threading.Barrier(2, timeout=2)
    calls = []
    request_thread = threading.get_ident()

    def route(*args):
        calls.append(("route", threading.get_ident()))
        rendezvous.wait()
        return {"selected_route": "engine"}

    def target(text, candidates, fallback, **kwargs):
        calls.append(("target", threading.get_ident()))
        rendezvous.wait()
        assert fallback == "alice-chief"
        assert kwargs["shared_tools"] == ["gmail_read"]
        return {"selected_bot_id": "alice-mail", "source": "jev"}

    monkeypatch.setattr(jev_routing, "decide_route", route)
    monkeypatch.setattr(jev_routing, "decide_bot_target", target)
    asyncio.run(consume(service))

    assert sorted(name for name, _ in calls) == ["route", "target"]
    assert all(tid != request_thread for _, tid in calls)
    assert observed[0]["hint"]["selected_bot_id"] == "alice-mail"


def test_slow_owner_does_not_block_other_chat_or_leak_target(chat, monkeypatch):
    service, observed, _, _ = chat
    started, release = threading.Event(), threading.Event()

    def target(text, candidates, fallback, **kwargs):
        if fallback == "alice-chief":
            started.set()
            assert release.wait(3), "test failed to release the blocked decision"
        selected = fallback.replace("-chief", "-mail")
        assert selected in {c["id"] for c in candidates}
        return {"selected_bot_id": selected, "source": "jev"}

    monkeypatch.setattr(jev_routing, "decide_bot_target", target)

    async def run():
        slow = asyncio.create_task(consume(service, "alice"))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            await asyncio.wait_for(consume(service, "bob"), timeout=1)
            assert not slow.done()
            assert [item["user"] for item in observed] == ["bob"]
            assert jev_stream_router._bot_target_hint.get() is None
        finally:
            release.set()
            await slow

    asyncio.run(run())
    for item in observed:
        assert item["hint"]["selected_bot_id"] == item["user"] + "-mail"
        assert item["context"]["selected_bot_id"] == item["user"] + "-mail"
        assert item["hint"]["request_text"] == item["user"] + " request"
    assert jev_stream_router._bot_target_hint.get() is None


def test_developer_http_wait_leaves_event_loop_responsive(chat, monkeypatch):
    service, observed, _, preset = chat
    started, release = threading.Event(), threading.Event()
    service.resolve_bot_for_user = lambda user, bid: {
        "id": bid, "owner": user, "policy": preset}

    def route(text, available, fallback):
        assert available == {"engine", "repo"}
        assert fallback == "repo"
        started.set()
        assert release.wait(3)
        return {"selected_route": "engine"}

    monkeypatch.setattr(jev_routing, "decide_route", route)

    async def run():
        task = asyncio.create_task(consume(service))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert not task.done()
            assert observed == []  # Never execute before routing completes.
        finally:
            release.set()
            await task

    asyncio.run(run())
    assert len(observed) == 1


@pytest.mark.parametrize("confidence,target,reason", [
    (0.95, "alice-mail", "accepted"),
    (0.10, "alice-mail", "low_confidence"),
    (0.99, "bob-mail", "illegal_bot"),
    (0.99, None, "jev_error"),
])
def test_parallel_target_keeps_existing_validation(
        chat, monkeypatch, confidence, target, reason):
    service, observed, _, _ = chat
    monkeypatch.setattr(jev_routing, "enabled_from_env", lambda: True)

    class Client:
        def decide(self, state, questions):
            if target is None:
                raise jev_routing.JevError("offline")
            assert list(questions) == ["target"]
            return {"model": "test", "answers": {"target": {
                "choice": target, "confidence": confidence}}}

    monkeypatch.setattr(jev_routing, "_active_jev_client", lambda timeout: Client())
    asyncio.run(consume(service))
    hint = observed[0]["hint"]
    assert hint["reason"] == reason
    assert hint["selected_bot_id"] == (
        "alice-mail" if reason == "accepted" else "alice-chief")


def test_cancel_during_routing_never_starts_executor_or_installs_late_hint(
        chat, monkeypatch):
    service, observed, _, _ = chat
    started, release, finished = (threading.Event() for _ in range(3))

    def target(*args, **kwargs):
        started.set()
        try:
            assert release.wait(3)
            return {"selected_bot_id": "alice-mail"}
        finally:
            finished.set()

    monkeypatch.setattr(jev_routing, "decide_bot_target", target)

    async def run():
        task = asyncio.create_task(consume(service))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            # A running synchronous HTTP request cannot be forcibly stopped.
            # Its eventual return must have no execution/context side effects.
            release.set()
            assert await asyncio.to_thread(finished.wait, 2)
        assert observed == []
        assert jev_stream_router._bot_target_hint.get() is None

    asyncio.run(run())


def test_fixed_fitness_route_does_not_start_jev_work(chat, monkeypatch):
    service, observed, _, _ = chat

    def unexpected(*args, **kwargs):
        raise AssertionError("fixed route must bypass Jev")

    monkeypatch.setattr(jev_routing, "decide_route", unexpected)
    monkeypatch.setattr(jev_routing, "decide_bot_target", unexpected)
    asyncio.run(consume(service, fitness_read_only=True))
    assert observed[0]["hint"] is None
