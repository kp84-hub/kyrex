"""Memory isolation, bounded context, and explicit user control."""

from types import SimpleNamespace

import chat_memory
import pytest


class FakeDoc:
    def __init__(self, bucket, key):
        self.bucket, self.id = bucket, key

    def collection(self, name):
        return FakeCollection(self.bucket, self.id + "/" + name)

    def get(self):
        value = self.bucket.get(self.id)
        return SimpleNamespace(id=self.id.split("/")[-1], exists=value is not None,
                               to_dict=lambda: value)

    def create(self, value):
        if self.id in self.bucket:
            raise ValueError("already exists")
        self.bucket[self.id] = value

    def delete(self):
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

    def stream(self):
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
