"""Actual Maps tool/host protocol, authorization and revocation regressions."""
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import patch
import pytest

os.environ.setdefault('GITHUB_CLIENT_ID', 'test-client')
os.environ.setdefault('GITHUB_CLIENT_SECRET', 'test-secret')
os.environ.setdefault('WEB_ALLOWED_GITHUB_USERNAME', 'test-user')
import chat_service as chat
import jev_stream_router as jev
import maps_routes as maps
from kyrex import toolbox


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setenv('GOOGLE_MAPS_API_KEY', 'private-routes-key')
    monkeypatch.setenv('KYREX_MAPS_ROUTE_OWNERS', 'keith')
    bot = {'id': 'chief', 'owner': 'keith', 'status': 'running', 'policy': {'*': 'deny', 'bot:delegate': 0}}
    session = chat.EngineSession.__new__(chat.EngineSession)
    session.maps_ctx = {'owner': 'keith', 'bot_id': 'chief'}
    session.allowed_tools = {'maps_route'}
    def resolve(owner, bot_id):
        assert owner == 'keith' and bot_id == 'chief'
        return dict(bot)
    monkeypatch.setattr(chat, 'resolve_bot_for_user', resolve)
    return session, bot


def test_engine_tool_request_roundtrips_through_actual_host_handler(rig, monkeypatch):
    session, _ = rig
    monkeypatch.setenv('KYREX_MAPS_HOST_TOOL', '1')
    monkeypatch.setenv('KYREX_SURFACE', 'Kyrex Chat')
    seen = []
    class HostSink:
        def write(self, line):
            frame = json.loads(line)
            assert frame['value'] == 'maps_route'
            assert 'private-routes-key' not in line
            approved, result = session._handle_maps_route(frame)
            seen.append(frame)
            toolbox._confirmation_results[frame['id']] = approved
            toolbox._confirmation_payloads[frame['id']] = result
            toolbox._pending_confirmations[frame['id']].set()
        def flush(self): pass
    with patch.object(maps, 'compute_route', return_value={'source': 'Google Maps Routes API', 'duration_minutes': 32}) as compute:
        with patch.object(toolbox.sys, 'stdout', HostSink()):
            result = toolbox.ToolBox(None).maps_route('Willow Spring, NC', 'Coquette, Raleigh, NC')
    assert result['status'] == 'ok' and result['duration_minutes'] == 32
    compute.assert_called_once_with('keith', 'Willow Spring, NC', 'Coquette, Raleigh, NC', None)
    assert seen[0]['id'] not in toolbox._pending_confirmations
    assert seen[0]['id'] not in toolbox._confirmation_payloads


def test_host_rechecks_restrictions_lifecycle_and_revoked_owner_grant(rig, monkeypatch):
    session, bot = rig
    for change in ({'status': 'stopped'}, {'policy': {'maps:route': 'deny'}}, {'owner': 'other'}):
        original = dict(bot); bot.update(change)
        with patch.object(maps, 'compute_route') as compute:
            approved, result = session._handle_maps_route({'origin': 'one', 'destination': 'two'})
            assert not approved and result['error_code'] == 'not_authorized'
            compute.assert_not_called()
        bot.clear(); bot.update(original)
    monkeypatch.setenv('KYREX_MAPS_ROUTE_OWNERS', '')
    approved, result = session._handle_maps_route({'origin': 'one', 'destination': 'two'})
    assert not approved


def test_host_turn_limit_prevents_unbounded_provider_calls(rig):
    session, _ = rig
    with patch.object(maps, 'compute_route', return_value={'duration_minutes': 1}) as compute:
        for _ in range(3):
            assert session._handle_maps_route({'origin': 'one', 'destination': 'two'})[0]
        approved, result = session._handle_maps_route({'origin': 'one', 'destination': 'two'})
        assert not approved and result['error_code'] == 'turn_limit'
        assert compute.call_count == 3


def test_forged_frame_without_session_grant_is_denied(rig):
    session, _ = rig; session.allowed_tools = set()
    with patch.object(maps, 'compute_route') as compute:
        assert not session._handle_maps_route({'origin': 'one', 'destination': 'two'})[0]
        compute.assert_not_called()


def test_unexpected_host_failure_does_not_leak_key_or_exception(rig):
    session, _ = rig
    with patch.object(maps, 'compute_route', side_effect=RuntimeError('private-routes-key')):
        approved, result = session._handle_maps_route({'origin': 'one', 'destination': 'two'})
    assert not approved
    assert 'private-routes-key' not in json.dumps(result)


def test_jev_sees_maps_as_available_only_for_authorized_owner(rig):
    _, bot = rig
    dev = SimpleNamespace(gmail_route_ready=lambda bot: False, email_calendar_route_ready=lambda bot: False)
    assert 'maps_route' in jev._shared_tools(dev, bot)
    assert 'maps_route' not in jev._shared_tools(dev, {**bot, 'owner': 'other'})


def test_tool_unavailable_on_non_host_surfaces(monkeypatch):
    monkeypatch.delenv('KYREX_MAPS_HOST_TOOL', raising=False)
    assert toolbox.ToolBox(None).maps_route('one', 'two')['status'] == 'error'


def test_api_key_is_removed_from_actual_engine_spawn_environment(rig, monkeypatch, tmp_path):
    from unittest.mock import MagicMock
    captured = {}
    def spawn(*args, **kwargs):
        captured.update(kwargs['env'])
        proc = MagicMock()
        proc.stdout = io.StringIO('')
        proc.stderr = io.StringIO('')
        proc.stdin = io.StringIO('')
        proc.poll.return_value = None
        return proc
    monkeypatch.setattr(chat.subprocess, 'Popen', spawn)
    monkeypatch.setattr(chat.EngineSession, '_wait_handshake', lambda self: None)
    session = chat.EngineSession(tmp_path, {'provider': 'openai', 'model': 'test', 'api_key': 'llm-key', 'base_url': '', 'headers': {}, 'profile': 'test'},
        {'bot_id': 'chief', 'allowed_tools': ['maps_route'], 'system_prompt': 'BOT IDENTITY',
         'maps_context': {'owner': 'keith', 'bot_id': 'chief'}})
    assert 'GOOGLE_MAPS_API_KEY' not in captured
    assert 'KYREX_MAPS_ROUTE_OWNERS' not in captured
    assert captured['KYREX_MAPS_HOST_TOOL'] == '1'
    assert 'maps_route' in captured['KYREX_ALLOWED_TOOLS']
    assert captured['KYREX_CHAT_SYSTEM_PROMPT'] == 'BOT IDENTITY'
    assert 'Google Maps' in captured['KYREX_CHAT_TOOL_CONTEXT']
    assert 'private-routes-key' not in captured['KYREX_CHAT_TOOL_CONTEXT']
    session.close()


def test_engine_schema_maps_tool_is_host_only_and_allowlist_gated(monkeypatch):
    from kyrex.core import PlaneExecute
    engine = PlaneExecute.__new__(PlaneExecute)
    engine.mcp = SimpleNamespace(get_tool_schemas=lambda: [])
    monkeypatch.setenv('KYREX_ALLOWED_TOOLS', 'maps_route')
    monkeypatch.delenv('KYREX_MAPS_HOST_TOOL', raising=False)
    assert not any(s['function']['name'] == 'maps_route' for s in engine._get_all_tools_schema())
    monkeypatch.setenv('KYREX_MAPS_HOST_TOOL', '1')
    assert any(s['function']['name'] == 'maps_route' for s in engine._get_all_tools_schema())
    monkeypatch.setenv('KYREX_ALLOWED_TOOLS', 'task_complete')
    assert not any(s['function']['name'] == 'maps_route' for s in engine._get_all_tools_schema())


def test_actual_bot_chat_stream_receives_owner_maps_grant_without_mutating_policy(rig, monkeypatch):
    import asyncio
    from pathlib import Path
    import test_chat_coordinator as base
    bot = base._bot('maps-chief', owner='keith', system_prompt='BOT IDENTITY',
                    policy={'bot:delegate': 0, '*': 'deny'})
    seen = []
    class Engine:
        def __init__(self, user, cid, workspace, bot_cfg=None):
            seen.append(bot_cfg)
            self.surface_context = None
            self.delegation_ctx = None
        def run_turn(self, text, on_token, cancel_check=None):
            return 'Maps result', None
        def interrupt(self): pass
        def close(self): pass
    monkeypatch.setattr(chat, '_get_engine_session', Engine)
    # Restore the real resolver; rig otherwise injects its one-Bot double.
    def resolve(owner, bot_id):
        assert owner == 'keith' and bot_id == 'maps-chief'
        return chat.bots.load_bots()[bot_id]
    monkeypatch.setattr(chat, 'resolve_bot_for_user', resolve)
    conv = chat.create_conversation('keith', bot_id='maps-chief')
    async def collect():
        return [f async for f in chat.stream_chat('keith', conv['conversation_id'],
                                                 'How long is the drive from Willow Spring to Coquette?')]
    frames = asyncio.run(collect())
    assert any(f.get('status') == 'complete' for f in frames)
    assert 'maps_route' in seen[0]['allowed_tools']
    assert seen[0]['maps_context'] == {'owner': 'keith', 'bot_id': 'maps-chief'}
    assert seen[0]['system_prompt'] == 'BOT IDENTITY'
    assert 'maps:route' not in chat.bots.load_bots()['maps-chief']['policy']


def test_host_tool_guidance_is_separate_from_bot_identity(monkeypatch):
    import core_bridge
    messages = []
    monkeypatch.setattr(core_bridge, '_BOT_PROMPT_APPLIED', False)
    monkeypatch.setenv('KYREX_CHAT_SYSTEM_PROMPT', 'BOT IDENTITY')
    monkeypatch.setenv('KYREX_CHAT_TOOL_CONTEXT', chat.MAPS_GUIDANCE)
    monkeypatch.setenv('KYREX_SURFACE', 'Kyrex Chat')
    engine = SimpleNamespace(session=SimpleNamespace(append=messages.append))
    core_bridge._apply_bot_system_prompt(engine)
    core_bridge._apply_bot_system_prompt(engine)
    assert len(messages) == 2
    assert messages[0]['content'] == 'BOT EXECUTION CONTEXT: BOT IDENTITY'
    assert messages[1]['content'].startswith('HOST TOOL CONTEXT: ')
    assert 'Google Maps' in messages[1]['content']
