import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import chat_api
import chat_delivery
import chat_service


@pytest.mark.parametrize("stop", [False, True])
def test_phone_disconnect_only_detaches_and_resume_observes_the_same_request(monkeypatch, stop):
    cid, rid = "1" * 32, "2" * 32
    calls = []
    saved = {"messages": []}
    monkeypatch.setattr(chat_delivery, "deliveries", {})
    monkeypatch.setattr(chat_api, "_active_streams", {})
    monkeypatch.setattr(chat_api, "_require_user", lambda request: request.user)
    monkeypatch.setattr(chat_service, "get_conversation", lambda user, cid: saved)

    async def run():
        finish = asyncio.Event()

        async def stream(owner, conversation, message, cancel, **kwargs):
            calls.append((owner, conversation, message))
            saved["messages"].append({"id": f"turn-{rid}-user", "role": "user"})
            yield {"type": "conversation", "conversation_id": conversation}
            await finish.wait()
            if cancel.is_set():
                yield {"type": "status", "status": "cancelled"}
            else:
                yield {"type": "status", "status": "complete", "content": "Original reply"}

        monkeypatch.setattr(chat_service, "stream_chat", stream)

        async def payload():
            return {"conversation_id": cid, "request_id": rid, "message": "Original request", "resumable": True}

        request = SimpleNamespace(user="alice", json=payload)
        response = await chat_api.chat(request)
        assert response.headers["x-kyrex-resumable"] == "1"
        assert (await anext(response.body_iterator)).startswith("id: 1\n")
        await response.body_iterator.aclose()
        assert rid in chat_api._active_streams

        with pytest.raises(HTTPException) as denied:
            await chat_api.resume_chat(cid, rid, SimpleNamespace(user="bob"))
        assert denied.value.status_code == 404

        again = await chat_api.chat(request)
        assert (await anext(again.body_iterator)).startswith("id: 1\n")
        await again.body_iterator.aclose()
        resumed = await chat_api.resume_chat(cid, rid, request, after=1)
        if stop:
            assert chat_api.cancel_request("alice", rid)
        finish.set()
        frames = [frame async for frame in resumed.body_iterator]
        assert len(frames) == 1 and frames[0].startswith("id: 2\n")
        assert ('"type": "cancelled"' if stop else "Original reply") in frames[0]
        assert calls == [("alice", cid, "Original request")]
        assert rid not in chat_api._active_streams
        chat_delivery.deliveries.clear()  # Replacement of the web process.
        with pytest.raises(HTTPException) as seen:
            await chat_api.chat(request)
        assert seen.value.status_code == 409
        assert len(calls) == 1

    asyncio.run(run())
