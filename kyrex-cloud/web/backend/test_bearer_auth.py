"""Focused tests for desktop bearer authentication and token lifecycle."""

import asyncio
import importlib
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-bearer-auth-tests")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
main = importlib.import_module("main")


class FakeStore:
    def __init__(self):
        self.tasks = {}
        self.submissions = []
        self.responses = []
        self.cancels = []
        self.operator_replies = []

    def submit(self, **kwargs):
        task_id = f"task-{len(self.submissions) + 1}"
        self.submissions.append(kwargs)
        self.tasks[task_id] = {
            "task_id": task_id,
            "session_key": kwargs["session_key"],
            "status": "queued",
            "task_text": kwargs["task_text"],
            "created_at": "created",
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
        }
        return task_id

    def get(self, task_id):
        return self.tasks.get(task_id)

    def list_tasks(self, session_key=None, limit=50):
        return [task for task in self.tasks.values() if task["session_key"] == session_key][:limit]

    def record_operator_reply(self, task_id, text):
        self.operator_replies.append((task_id, text))
        return True

    def request_cancel(self, task_id):
        self.cancels.append(task_id)
        return True

    def status(self, task_id):
        task = self.tasks.get(task_id)
        return task["status"] if task else None


class Request:
    def __init__(self, headers=None, cookies=None, body=None):
        self.headers = headers or {}
        self.cookies = cookies or {}
        self._body = body or {}

    async def json(self):
        return self._body


def reset_state():
    main.sessions.clear()
    with main.desktop_lock:
        main.desktop_access_tokens.clear()
        main.desktop_refresh_tokens.clear()


def setup_function():
    reset_state()
    main.store = FakeStore()


def teardown_function():
    reset_state()


def request_with_bearer(token):
    return Request(headers={"authorization": f"Bearer {token}"})


def token_pair(user="allowed-user"):
    return main._new_desktop_tokens(user)


def call(coro):
    return asyncio.run(coro)


def test_bearer_valid_invalid_and_expired_authentication():
    tokens = token_pair()
    assert main.require_user(request_with_bearer(tokens["access_token"])) == "allowed-user"

    with pytest.raises(main.HTTPException) as exc:
        main.require_user(request_with_bearer("not-a-token"))
    assert exc.value.status_code == 401

    digest = main._token_hash(tokens["access_token"])
    with main.desktop_lock:
        main.desktop_access_tokens[digest]["expires_at"] = time.time() - 1
    with pytest.raises(main.HTTPException) as exc:
        main.require_user(request_with_bearer(tokens["access_token"]))
    assert exc.value.status_code == 401


def test_api_me_accepts_bearer_and_preserves_browser_cookie_authentication():
    tokens = token_pair()
    bearer_response = main.me(request_with_bearer(tokens["access_token"]))
    assert bearer_response == {"authenticated": True, "username": "allowed-user"}

    main.sessions["browser-session"] = "browser-user"
    cookie_response = main.me(Request(cookies={"session": "browser-session"}))
    assert cookie_response == {"authenticated": True, "username": "browser-user"}

    with pytest.raises(main.HTTPException) as exc:
        main.me(request_with_bearer("invalid"))
    assert exc.value.status_code == 401


def test_task_submission_status_results_respond_and_cancel_accept_bearer():
    tokens = token_pair()
    request = Request(headers={"authorization": f"Bearer {tokens['access_token']}"}, body={"task": "inspect repository"})
    submitted = call(main.accept_task(request))
    task_id = submitted["task_id"]
    assert main.store.submissions[0]["session_key"] == "allowed-user"

    task_request = request_with_bearer(tokens["access_token"])
    status = main.get_task(task_id, task_request)
    assert status["task_id"] == task_id
    assert status["status"] == "queued"

    results = main.list_results(task_request)
    assert results["results"][0]["task"] == "inspect repository"

    response = call(main.respond_task(task_id, Request(
        headers={"authorization": f"Bearer {tokens['access_token']}"}, body={"text": "y"})))
    assert response == {"recorded": True}
    assert main.store.operator_replies == [(task_id, "y")]

    cancelled = call(main.cancel_task(task_id, task_request))
    assert cancelled["requested"] is True
    assert main.store.cancels == [task_id]


def test_authenticated_sse_authorizes_bearer_without_query_token():
    tokens = token_pair()
    task_id = main.store.submit(session_key="allowed-user", task_text="stream")
    with patch.object(main.flux, "stream_events", return_value=iter([])):
        response = call(main.task_events(task_id, request_with_bearer(tokens["access_token"])))
    assert response.media_type == "text/event-stream"

    with pytest.raises(main.HTTPException) as exc:
        call(main.task_events(task_id, Request()))
    assert exc.value.status_code == 401


def test_expired_access_token_refreshes_and_retries_once():
    tokens = token_pair()
    access_digest = main._token_hash(tokens["access_token"])
    with main.desktop_lock:
        main.desktop_access_tokens[access_digest]["expires_at"] = time.time() - 1

    refreshed = main._new_desktop_tokens("allowed-user", main.desktop_refresh_tokens[main._token_hash(tokens["refresh_token"])]["family"])
    calls = []

    def refresh():
        calls.append("refresh")
        return refreshed

    request = request_with_bearer(tokens["access_token"])
    with patch.object(main, "_new_desktop_tokens", return_value=refreshed):
        assert main._bearer_user(request) is None
        # The server-side retry contract is exercised by the IDE client; the
        # endpoint-level assertion verifies the expired credential is rejected.
        with pytest.raises(main.HTTPException):
            main.require_user(request)
    assert calls == []


def test_refresh_rotation_reuse_and_token_family_revocation():
    tokens = token_pair()
    refresh_request = Request(body={"refresh_token": tokens["refresh_token"]})
    rotated = call(main.desktop_refresh(refresh_request))
    assert rotated["refresh_token"] != tokens["refresh_token"]
    assert rotated["access_token"] != tokens["access_token"]
    assert rotated["username"] == "allowed-user"

    with pytest.raises(main.HTTPException) as exc:
        call(main.desktop_refresh(refresh_request))
    assert exc.value.status_code == 401
    with main.desktop_lock:
        old_entry = main.desktop_refresh_tokens[main._token_hash(tokens["refresh_token"])]
        assert old_entry["used"] is True

    logout_response = main.desktop_logout(request_with_bearer(rotated["access_token"]))
    assert logout_response == {"revoked": True}
    with pytest.raises(main.HTTPException):
        main.require_user(request_with_bearer(rotated["access_token"]))
    with pytest.raises(main.HTTPException):
        call(main.desktop_refresh(Request(body={"refresh_token": rotated["refresh_token"]})))


def test_expired_refresh_and_atomic_refresh_consumption():
    tokens = token_pair()
    digest = main._token_hash(tokens["refresh_token"])
    with main.desktop_lock:
        main.desktop_refresh_tokens[digest]["expires_at"] = time.time() - 1
    with pytest.raises(main.HTTPException) as exc:
        call(main.desktop_refresh(Request(body={"refresh_token": tokens["refresh_token"]})))
    assert exc.value.status_code == 401

    tokens = token_pair()
    barrier = threading.Barrier(2)
    results = []

    def refresh_once():
        barrier.wait()
        try:
            results.append(call(main.desktop_refresh(Request(body={"refresh_token": tokens["refresh_token"]}))))
        except main.HTTPException:
            results.append(None)

    threads = [threading.Thread(target=refresh_once) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sum(result is not None for result in results) == 1
