"""A quiet durable Bot turn keeps the Chat SSE connection alive."""

import asyncio
import os
import sys

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-chat-keepalive-tests")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")

_HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (_HERE, os.path.dirname(os.path.dirname(_HERE))):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import chat_api  # noqa: E402


@pytest.mark.asyncio
async def test_quiet_chat_turn_pings_then_delivers_terminal_frame():
    async def slow_turn():
        yield chat_api._sse_frame({"type": "conversation", "conversation_id": "c"})
        await asyncio.sleep(0.035)
        yield chat_api._sse_frame({"type": "done", "content": "ready"})

    frames = [frame async for frame in chat_api._with_chat_keepalive(
        slow_turn(), interval=0.01)]
    assert frames[0].startswith('data: {"type": "conversation"')
    assert ": ping\n\n" in frames[1:-1]
    assert frames[-1].startswith('data: {"type": "done"')


@pytest.mark.asyncio
async def test_disconnected_viewer_cancels_pending_read():
    cancelled = asyncio.Event()

    async def slow_turn():
        try:
            await asyncio.Event().wait()
            yield "unreachable"
        finally:
            cancelled.set()

    stream = chat_api._with_chat_keepalive(slow_turn(), interval=0.01)
    assert await stream.__anext__() == ": ping\n\n"
    await stream.aclose()
    assert cancelled.is_set()
