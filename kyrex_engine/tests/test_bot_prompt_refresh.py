from types import SimpleNamespace
import importlib
import sys
from pathlib import Path
from kyrex.session.tree import TreeSessionManager


def test_saved_bot_context_refreshes_after_restart_without_losing_history(tmp_path,monkeypatch):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    bridge=importlib.import_module('core_bridge')
    session=TreeSessionManager(str(tmp_path))
    session.append({'role':'system','content':'Unrelated engine rules'})
    session.append({'role':'system','content':'BOT EXECUTION CONTEXT: date 2026-10-08; old guidance'})
    session.append({'role':'user','content':'Graph my workout'})
    session.save('main')
    restarted=TreeSessionManager(str(tmp_path)); restarted.load('main')
    monkeypatch.setattr(bridge,'_BOT_PROMPT_APPLIED',False)
    monkeypatch.setenv('KYREX_CHAT_SYSTEM_PROMPT','date 2026-10-09; fresh workout guidance')
    bridge._apply_bot_system_prompt(SimpleNamespace(session=restarted))
    bridge._apply_bot_system_prompt(SimpleNamespace(session=restarted))
    assert len(restarted.history)==3
    assert restarted.history[0]['content']=='Unrelated engine rules'
    assert restarted.history[1]['content']=='BOT EXECUTION CONTEXT: date 2026-10-09; fresh workout guidance'
    assert restarted.history[2]['content']=='Graph my workout'
    restarted.save('main')
    reopened=TreeSessionManager(str(tmp_path)); reopened.load('main')
    assert reopened.history==restarted.history
