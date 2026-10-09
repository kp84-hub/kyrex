from types import SimpleNamespace
from kyrex.core import PlaneExecute
from kyrex.toolbox import ToolBox
import json
import pytest


def test_github_schema_is_chat_only(monkeypatch):
    engine = object.__new__(PlaneExecute)
    engine.mcp = SimpleNamespace(get_tool_schemas=lambda: [])
    monkeypatch.setenv('KYREX_ALLOWED_TOOLS', 'github_read,task_complete')
    monkeypatch.delenv('KYREX_SURFACE', raising=False)
    assert 'github_read' not in {s['function']['name'] for s in engine._get_all_tools_schema()}
    monkeypatch.setenv('KYREX_SURFACE', 'Kyrex Chat')
    assert 'github_read' in {s['function']['name'] for s in engine._get_all_tools_schema()}
    monkeypatch.setenv('KYREX_ALLOWED_TOOLS', 'task_complete')
    assert 'github_read' not in {s['function']['name'] for s in engine._get_all_tools_schema()}


def test_non_chat_cannot_emit_github_host_request(monkeypatch, capsys):
    monkeypatch.delenv('KYREX_SURFACE', raising=False)
    toolbox = object.__new__(ToolBox)
    assert 'available in Kyrex Chat' in toolbox.github_read('status')['error']
    assert not capsys.readouterr().out


def test_fitness_schema_is_chat_only_and_policy_masked(monkeypatch):
    engine = object.__new__(PlaneExecute)
    engine.mcp = SimpleNamespace(get_tool_schemas=lambda: [])
    monkeypatch.setenv('KYREX_ALLOWED_TOOLS', 'fitness_read,task_complete')
    monkeypatch.delenv('KYREX_SURFACE', raising=False)
    assert 'fitness_read' not in {s['function']['name'] for s in engine._get_all_tools_schema()}
    monkeypatch.setenv('KYREX_SURFACE', 'Kyrex Chat')
    assert 'fitness_read' in {s['function']['name'] for s in engine._get_all_tools_schema()}
    monkeypatch.setenv('KYREX_ALLOWED_TOOLS', 'task_complete')
    assert 'fitness_read' not in {s['function']['name'] for s in engine._get_all_tools_schema()}


def test_non_chat_cannot_emit_fitness_host_request(monkeypatch, capsys):
    monkeypatch.delenv('KYREX_SURFACE', raising=False)
    toolbox = object.__new__(ToolBox)
    assert 'available in Kyrex Chat' in toolbox.fitness_read()['error']
    assert not capsys.readouterr().out

@pytest.mark.parametrize('zone',[None,'Europe/London'])
def test_fitness_local_day_reaches_host_and_returns_session_metrics(monkeypatch,zone):
    import kyrex.toolbox as module
    monkeypatch.setenv('KYREX_SURFACE','Kyrex Chat')
    frames=[]
    class HostChannel:
        def write(self,text):
            frame=json.loads(text); frames.append(frame)
            module._confirmation_results[frame['id']]=True
            module._confirmation_payloads[frame['id']]={'session_metrics':{'heart_rate_avg_bpm':132}}
            module._pending_confirmations[frame['id']].set()
        def flush(self): pass
    monkeypatch.setattr(module.sys,'stdout',HostChannel())
    kwargs={'start':'2026-10-08','end':'2026-10-08','collection':'workout'}
    if zone: kwargs['timezone']=zone
    result=object.__new__(ToolBox).fitness_read(**kwargs)
    assert result['session_metrics']['heart_rate_avg_bpm']==132
    assert frames[0]['timezone']==(zone or 'America/New_York')
    assert frames[0]['start']==frames[0]['end']=='2026-10-08'
    assert frames[0]['collection']=='workout'
    assert frames[0]['id'] not in module._pending_confirmations
