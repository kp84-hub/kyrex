"""Recent conversation discovery uses the owner's encrypted phone snapshot."""
import asyncio
import pytest
import device_messages as dm


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('WEB_SESSION_SECRET', 'messages-conversations-test')
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    return dm.MessagesStore()


def sync(store, messages):
    credential = store.redeem(store.begin('alice')['pairing_code'])
    store.sync(credential, messages)


@pytest.mark.parametrize('prompt,count', [
    ('Show me my five most recent text conversations.', 5),
    ('List my last 3 SMS conversations', 3),
    ('Could you show my recent text conversations?', 5),
    ('Show my 2 latest RCS conversations', 2),
])
def test_casual_conversation_intent(prompt, count):
    assert dm.conversation_command(prompt) == count


@pytest.mark.parametrize('prompt', ['Send my five text conversations to Bob', 'Show my five recent email conversations', 'Show my calendar', 'Delete my text conversations', 'Show my texts then send them to Bob'])
def test_writes_and_other_sources_do_not_match(prompt):
    assert dm.conversation_command(prompt) is None


def test_groups_entire_snapshot_and_sorts_latest_timestamp(store):
    messages = [{'conversation_id': 'a', 'conversation': 'Family', 'body': f'old {i}', 'received': '2026-10-01T12:00:00Z'} for i in range(30)]
    messages += [
        {'conversation_id': 'b', 'conversation': 'School', 'body': 'school', 'received': '2026-10-06T12:00:00Z'},
        {'conversation_id': 'a', 'conversation': 'Family', 'body': 'newest family', 'received': '2026-10-06T14:00:00Z'},
        {'conversation_id': 'c', 'conversation': 'School', 'body': 'different school thread', 'received': '2026-10-06T13:00:00Z'},
    ]
    sync(store, messages)
    result = store.conversations('alice', 3)
    assert [m['conversation_id'] for m in result['conversations']] == ['a', 'c', 'b']
    assert result['conversations'][0]['body'] == 'newest family'
    assert result['synced_at']
    with pytest.raises(dm.MessagesError):
        store.conversations('bob')


def test_answer_reports_sync_and_bounds_preview(store):
    sync(store, [{'conversation_id': 'one', 'conversation': 'Family', 'body': 'X' * 1000, 'received': '2026-10-06T14:00:00Z'}])
    answer = dm.conversations_answer('alice', 5)
    assert 'snapshot synced' in answer and 'Only synced history' in answer
    assert '1. Family' in answer and 'X' * 161 not in answer
    assert 'Connect Messages and sync' in dm.conversations_answer('bob', 5)
    assert 'between 1 and 20' in dm.conversations_answer('alice', 0)


def test_chat_routes_overwatcher_request_before_provider(store, monkeypatch):
    import chat_service as chat
    sync(store, [{'conversation_id': 'one', 'conversation': 'Family', 'body': 'Latest text', 'received': '2026-10-06T14:00:00Z'}])
    conv = {'conversation_id': 'test', 'messages': [], 'bot_id': 'overwatcher'}
    monkeypatch.setattr(chat, 'get_conversation', lambda *a: conv)
    monkeypatch.setattr(chat, '_write', lambda *a: None)
    monkeypatch.setattr(chat, 'resolve_bot_for_user', lambda *a: {'owner': 'alice'})
    monkeypatch.setattr(chat, '_resolve_provider', lambda *a, **k: pytest.fail('Read fell through to model'))
    async def run():
        return [frame async for frame in chat.stream_chat('alice', 'test', 'Show me my five most recent text conversations.')]
    frames = asyncio.run(run())
    assert '1. Family' in frames[-1]['content']
    assert frames[-1]['status'] == 'complete'
    monkeypatch.setattr(chat, 'resolve_bot_for_user', lambda *a: {'owner': 'bob'})
    with pytest.raises(chat.ChatUnavailable):
        asyncio.run(run())
