import json
from types import SimpleNamespace

from kyrex.core import PlaneExecute
from kyrex.toolbox import ToolBox


def test_maps_tool_is_chat_only_and_allowlist_filtered(monkeypatch, capsys):
    engine = object.__new__(PlaneExecute)
    engine.mcp = SimpleNamespace(get_tool_schemas=lambda: [])
    monkeypatch.setenv('KYREX_ALLOWED_TOOLS', 'maps_route,task_complete')
    monkeypatch.delenv('KYREX_SURFACE', raising=False)
    assert 'maps_route' not in {s['function']['name'] for s in engine._get_all_tools_schema()}
    assert 'available in Kyrex Chat' in object.__new__(ToolBox).maps_route('home','mall')['error']
    assert not capsys.readouterr().out
    monkeypatch.setenv('KYREX_SURFACE', 'Kyrex Chat')
    assert 'maps_route' in {s['function']['name'] for s in engine._get_all_tools_schema()}
    monkeypatch.setenv('KYREX_ALLOWED_TOOLS', 'task_complete')
    assert 'maps_route' not in {s['function']['name'] for s in engine._get_all_tools_schema()}


def test_maps_host_round_trip_preserves_setup_error(monkeypatch):
    import kyrex.toolbox as module
    monkeypatch.setenv('KYREX_SURFACE','Kyrex Chat')
    frames = []
    payload = {'status':'unavailable','error_type':'not_configured','error':'Set up Routes','maps_url':'https://www.google.com/maps/'}
    class Host:
        def write(self, text):
            frame = json.loads(text)
            frames.append(frame)
            module._confirmation_results[frame['id']] = False
            module._confirmation_payloads[frame['id']] = payload
            module._pending_confirmations[frame['id']].set()
        def flush(self): pass
    monkeypatch.setattr(module.sys, 'stdout', Host())
    assert object.__new__(ToolBox).maps_route('Willow Spring, NC','Mall') == payload
    assert frames[0]['origin'] == 'Willow Spring, NC'
    assert frames[0]['value'] == 'maps_route' and 'owner' not in frames[0]
    assert frames[0]['id'] not in module._pending_confirmations
