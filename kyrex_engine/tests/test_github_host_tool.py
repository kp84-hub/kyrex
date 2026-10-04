from types import SimpleNamespace
from kyrex.core import PlaneExecute
from kyrex.toolbox import ToolBox


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
