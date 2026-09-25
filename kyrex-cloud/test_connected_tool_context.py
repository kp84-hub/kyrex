"""Worker-process coverage for owner-connected Calendar task authority."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_CLOUD = os.path.dirname(os.path.abspath(__file__))
if _CLOUD not in sys.path:
    sys.path.insert(0, _CLOUD)

import connected_tool_context as connected  # noqa: E402
import connectors  # noqa: E402


class _Store:
    def __init__(self, read=True, write=True):
        self.read = read
        self.write = write

    def scope_granted(self, owner, scope, provider="google"):
        return (owner == "alice" and self.read
                and scope == connectors.GOOGLE_CALENDAR_READ_SCOPE)

    def calendar_write_available(self, owner):
        return owner == "alice" and self.write


class _Ctx:
    def __init__(self):
        self.bot_owner = "alice"
        self.policy = {"fs:read": 0}


def _serve():
    def build_context(session_key, executor_prefix="repo",
                      allow_bot_resolution=True):
        return _Ctx()
    return SimpleNamespace(build_context=build_context)


def test_worker_overlay_is_task_local_and_repo_unchanged(monkeypatch):
    monkeypatch.setattr(connectors, "default_store", lambda: _Store())
    monkeypatch.setattr(connected, "_installed", False)
    serve = _serve()
    connected.install(serve)

    read_ctx = serve.build_context("bot", "calendar")
    create_ctx = serve.build_context("bot", "cal_write")
    delete_ctx = serve.build_context("bot", "cal_edit")
    repo_ctx = serve.build_context("bot", "repo")

    assert read_ctx.policy == {"fs:read": 0, "cal:list": 0}
    assert create_ctx.policy == {"fs:read": 0, "cal:create": 0}
    assert delete_ctx.policy == {"fs:read": 0, "cal:delete": 2}
    assert repo_ctx.policy == {"fs:read": 0}


def test_worker_overlay_fails_closed_without_connector_scope(monkeypatch):
    monkeypatch.setattr(connectors, "default_store",
                        lambda: _Store(read=False, write=False))
    monkeypatch.setattr(connected, "_installed", False)
    serve = _serve()
    connected.install(serve)

    assert serve.build_context("bot", "calendar").policy == {"fs:read": 0}
    assert serve.build_context("bot", "cal_write").policy == {"fs:read": 0}
    assert serve.build_context("bot", "cal_edit").policy == {"fs:read": 0}
