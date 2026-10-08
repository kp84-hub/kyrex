"""The bridge's outcome, not receipt of chat_done, determines success."""
import json
from pathlib import Path
import pytest
from headless_agent import HeadlessAgent
from git_workflow import record_agent_result


@pytest.mark.parametrize('outcome,terminal,failed', [
    ('complete', True, False), ('answered', True, False),
    ('incomplete', False, True), ('max_recursion', False, True),
    ('provider_error', False, True), ('interrupted', True, True),
    (None, True, True), ('complete', False, True),
])
def test_headless_outcome_drives_result(outcome, terminal, failed, monkeypatch):
    agent = HeadlessAgent(Path('bridge'), Path('repo'))
    monkeypatch.setattr(agent, '_shutdown', lambda: None)
    agent.out_q.put(('stdout', json.dumps({'type':'chat_done', 'content':'progress or final', 'outcome':outcome, 'terminal':terminal})))
    agent.out_q.put(('stdout_closed', None))
    agent.run()
    result = {}
    assert bool(record_agent_result(result, agent)) is failed
    assert result['outcome'] == outcome
    if failed:
        assert result['status'] == 'agent_failed'
        assert result['partial_response'] == 'progress or final'
        assert 'recovery' in result['final_response']
    else:
        assert result['final_response'] == 'progress or final'


def test_bridge_error_cannot_be_overridden_by_chat_done(monkeypatch):
    agent = HeadlessAgent(Path('bridge'), Path('repo'))
    monkeypatch.setattr(agent, '_shutdown', lambda: None)
    for message in [{'type':'error','content':'protocol failure'}, {'type':'chat_done','content':'Done','outcome':'complete','terminal':True}]:
        agent.out_q.put(('stdout', json.dumps(message)))
    agent.out_q.put(('stdout_closed', None))
    agent.run()
    assert record_agent_result({}, agent)


@pytest.mark.parametrize('outcome', ['incomplete','max_recursion','provider_error','interrupted',None])
def test_failed_git_agent_never_publishes_or_removes_partial_work(tmp_path, monkeypatch, capsys, outcome):
    import git_workflow
    from types import SimpleNamespace as NS
    partial = tmp_path/'partial.py'
    def prepare(args, branch):
        def cleanup():
            assert args.keep_workdir is True
        return tmp_path, 'https://example.invalid/repo', cleanup
    class Agent:
        chat_done_seen = True
        terminal = outcome == 'interrupted'
        final_response = 'I was editing the parser </invoke>'
        errors = []
        approvals = []
        tool_calls = []
        execution_error = False
        def __init__(self, *args, **kwargs):
            self.outcome = outcome
        def start(self, task):
            partial.write_text('unfinished but preserved')
            return True
        def run(self):
            pass
    monkeypatch.setattr('sys.argv', ['workflow','--task','fix calendar','--rift',str(tmp_path)])
    monkeypatch.setattr(git_workflow,'find_bridge_script',lambda _:Path('bridge'))
    monkeypatch.setattr(git_workflow,'prepare_workspace',prepare)
    monkeypatch.setattr(git_workflow,'HeadlessAgent',Agent)
    monkeypatch.setattr(git_workflow,'RESULTS_DIR',tmp_path/'results')
    def forbidden(*args, **kwargs):
        pytest.fail('An incomplete task must not commit, push, or open a PR')
    monkeypatch.setattr(git_workflow,'commit_and_push',forbidden)
    monkeypatch.setattr(git_workflow,'open_pull_request',forbidden)
    git_workflow.main()
    summary = json.loads(next((tmp_path/'results').glob('*.json')).read_text())
    assert summary['status'] == 'agent_failed'
    assert '</invoke>' not in summary['final_response']
    assert partial.read_text() == 'unfinished but preserved'
    from task_store import CloudTaskStore
    store = CloudTaskStore(db_path=tmp_path/'tasks.db')
    task = store.submit(session_key='dev',task_text='fix calendar')
    assert store.complete(task,summary) == 'failed'
