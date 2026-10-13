"""Draft tooling is Chat-only, policy-masked, and contains no send action."""
import json
from types import SimpleNamespace

import pytest
from kyrex.core import PlaneExecute
from kyrex.toolbox import ToolBox
import kyrex.toolbox as module


def test_schema_only_on_granted_chat_surface(monkeypatch):
    engine = object.__new__(PlaneExecute)
    engine.mcp = SimpleNamespace(get_tool_schemas=lambda: [])
    monkeypatch.setenv('KYREX_ALLOWED_TOOLS', 'email_draft,task_complete')
    monkeypatch.delenv('KYREX_SURFACE', raising=False)
    assert 'email_draft' not in {s['function']['name'] for s in engine._get_all_tools_schema()}
    monkeypatch.setenv('KYREX_SURFACE', 'Kyrex Chat')
    schema = next(s['function'] for s in engine._get_all_tools_schema() if s['function']['name'] == 'email_draft')
    assert schema['parameters']['properties']['action']['enum'] == ['prepare', 'status', 'replies']
    monkeypatch.setenv('KYREX_ALLOWED_TOOLS', 'task_complete')
    assert 'email_draft' not in {s['function']['name'] for s in engine._get_all_tools_schema()}


def test_local_tool_cannot_request_host_draft(monkeypatch, capsys):
    monkeypatch.delenv('KYREX_SURFACE', raising=False)
    assert 'available in Kyrex Chat' in object.__new__(ToolBox).email_draft()['error']
    assert not capsys.readouterr().out


@pytest.mark.parametrize('approved', [True, False])
def test_tool_frame_is_only_a_review_request(monkeypatch, approved):
    monkeypatch.setenv('KYREX_SURFACE', 'Kyrex Chat')
    frames = []
    payload = {'id': 'draft-one', 'state': 'draft', 'approval_required': True} if approved else {'error': 'Read the selected email first.'}
    class Host:
        def write(self, text):
            frame = json.loads(text); frames.append(frame)
            module._confirmation_results[frame['id']] = approved
            module._confirmation_payloads[frame['id']] = payload
            module._pending_confirmations[frame['id']].set()
        def flush(self): pass
    monkeypatch.setattr(module.sys, 'stdout', Host())
    result = object.__new__(ToolBox).email_draft('prepare', 'office@example.com', 'Appointment', 'Tuesday availability?', reply=True)
    assert result == payload
    assert frames[0]['value'] == 'email_draft' and frames[0]['reply'] is True
    assert 'decision' not in frames[0] and 'approved' not in frames[0]
