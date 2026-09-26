"""Memory isolation, bounded context, and explicit user control."""

import asyncio
import json
from types import SimpleNamespace

import chat_memory
import chat_service
import pytest


class FakeDoc:
    def __init__(self, bucket, key):
        self.bucket, self.id = bucket, key

    def collection(self, name):
        return FakeCollection(self.bucket, self.id + "/" + name)

    def get(self, timeout=None):
        value = self.bucket.get(self.id)
        return SimpleNamespace(id=self.id.split("/")[-1], exists=value is not None,
                               to_dict=lambda: value)

    def create(self, value, timeout=None):
        if self.id in self.bucket:
            raise ValueError("already exists")
        self.bucket[self.id] = value

    def delete(self, timeout=None):
        self.bucket.pop(self.id, None)


class FakeCollection:
    def __init__(self, bucket, prefix):
        self.bucket, self.prefix, self.count = bucket, prefix, None

    def document(self, key):
        return FakeDoc(self.bucket, self.prefix + "/" + key)

    def order_by(self, field):
        assert field == "created_at"
        return self

    def limit(self, count):
        self.count = count
        return self

    def stream(self, timeout=None):
        docs = [FakeDoc(self.bucket, key).get() for key in sorted(self.bucket)
                if key.startswith(self.prefix + "/")]
        return iter(docs[:self.count])


def test_memory_is_owner_scoped_idempotent_and_deletable(monkeypatch):
    bucket = {}
    client = SimpleNamespace(collection=lambda name: FakeCollection(bucket, name))
    monkeypatch.setattr(chat_memory, "_database", lambda: client)
    monkeypatch.setattr(chat_memory, "configured", lambda: True)

    first = chat_memory.remember("alice", "  I prefer  short  replies. ", identity="turn-1")
    assert first["text"] == "I prefer short replies."
    assert chat_memory.remember("alice", "I prefer short replies.", identity="turn-1") == first
    assert chat_memory.list_memories("bob") == []
    assert "I prefer short replies." in chat_memory.context("alice")
    assert "I prefer short replies." not in chat_memory.context("bob")
    assert chat_memory.forget("bob", first["id"]) is False
    assert chat_memory.forget("alice", first["id"]) is True
    assert chat_memory.list_memories("alice") == []


def test_memory_has_length_count_and_id_bounds(monkeypatch):
    bucket = {}
    client = SimpleNamespace(collection=lambda name: FakeCollection(bucket, name))
    monkeypatch.setattr(chat_memory, "_database", lambda: client)
    with pytest.raises(chat_memory.MemoryError):
        chat_memory.remember("alice", "x" * (chat_memory.MAX_FACT_CHARS + 1))
    with pytest.raises(chat_memory.MemoryError):
        chat_memory.forget("alice", "../bob")
    for n in range(chat_memory.MAX_ITEMS):
        chat_memory.remember("alice", f"Fact {n}")
    with pytest.raises(chat_memory.MemoryError, match="full"):
        chat_memory.remember("alice", "one more")


def test_configuration_explains_malformed_json_and_project_mismatch(monkeypatch):
    monkeypatch.setattr(chat_memory, "_client", None)
    monkeypatch.setenv("KYREX_FIRESTORE_PROJECT_ID", "kyrex-chat")
    monkeypatch.setenv("KYREX_FIRESTORE_SERVICE_ACCOUNT_JSON", "-----BEGIN PRIVATE KEY-----")
    with pytest.raises(chat_memory.MemoryError, match="not valid JSON"):
        chat_memory._database()

    monkeypatch.setenv("KYREX_FIRESTORE_SERVICE_ACCOUNT_JSON", json.dumps({
        "type": "service_account", "token_uri": "https://oauth2.googleapis.com/token",
        "project_id": "kyrex-chat-example", "client_email": "x@example.com",
        "private_key": "not-a-real-key",
    }))
    with pytest.raises(chat_memory.MemoryError, match="project ID differs"):
        chat_memory._database()

    monkeypatch.setenv("KYREX_FIRESTORE_SERVICE_ACCOUNT_JSON", json.dumps({
        "type": "service_account", "project_id": "kyrex-chat",
        "private_key": "not-a-real-key",
    }))
    with pytest.raises(chat_memory.MemoryError, match="not a complete"):
        chat_memory._database()


def test_explicit_chat_memory_survives_a_new_conversation(monkeypatch, tmp_path):
    bucket = {}
    client = SimpleNamespace(collection=lambda name: FakeCollection(bucket, name))
    monkeypatch.setattr(chat_memory, "_database", lambda: client)
    monkeypatch.setattr(chat_memory, "configured", lambda: True)
    monkeypatch.setattr(chat_service, "_data_dir", lambda: tmp_path)

    first = chat_service.create_conversation("alice")["conversation_id"]
    async def run(cid, message):
        return [frame async for frame in chat_service.stream_chat("alice", cid, message)]

    answer = asyncio.run(run(first, "Remember that I prefer short replies"))
    assert answer[-1]["status"] == "complete"
    assert "I prefer short replies" in answer[-1]["content"]

    second = chat_service.create_conversation("alice")["conversation_id"]
    listed = asyncio.run(run(second, "What do you remember about me?"))
    assert "I prefer short replies" in listed[-1]["content"]
    assert "I prefer short replies" in chat_memory.context("alice")
    assert chat_memory.list_memories("bob") == []
