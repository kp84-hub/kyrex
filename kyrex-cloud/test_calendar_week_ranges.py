"""Week intent must survive routing, local bounds and the production reader."""
from datetime import datetime
from types import SimpleNamespace

import pytest

import calendar_windows as cw
import connectors
import job_contracts
import serve
import task_store
from task_store import CloudTaskStore


def test_screenshot_next_week_request_has_its_own_window():
    assert serve.natural_calendar_command('Show me my calendar next week') == 'calendar: next-week'


@pytest.mark.parametrize('text,command', [
    ('Show my calendar for this week', 'calendar: this-week'),
    ('What do I have next week?', 'calendar: next-week'),
    ('Check my schedule current week', 'calendar: this-week'),
    ('Show my calendar upcoming week', 'calendar: week'),
    ('Show my calendar', 'calendar: week'),
])
def test_week_phrases_keep_distinct_meanings(text, command):
    assert serve.natural_calendar_command(text) == command
    assert serve.resolve_executor(command) == ('calendar', command, None)


@pytest.mark.parametrize('now,start,end', [
    (datetime(2026, 10, 16, 20, 30), '2026-10-19T00:00:00-04:00', '2026-10-26T00:00:00-04:00'),
    (datetime(2026, 10, 18, 23, 59), '2026-10-19T00:00:00-04:00', '2026-10-26T00:00:00-04:00'),
    (datetime(2026, 10, 19, 0, 1), '2026-10-26T00:00:00-04:00', '2026-11-02T00:00:00-05:00'),
    (datetime(2026, 2, 27, 12), '2026-03-02T00:00:00-05:00', '2026-03-09T00:00:00-04:00'),
    (datetime(2026, 12, 27, 12), '2026-12-28T00:00:00-05:00', '2027-01-04T00:00:00-05:00'),
    (datetime.fromisoformat('2026-10-19T00:30:00+00:00'),
     '2026-10-19T00:00:00-04:00', '2026-10-26T00:00:00-04:00'),
])
def test_next_week_is_next_monday_through_exclusive_following_monday(now, start, end):
    assert cw.window_bounds('next-week', now=now) == ('Next Week', start, end)


def test_current_week_and_legacy_rolling_week_keep_correct_bounds_and_labels():
    now = datetime(2026, 10, 16, 20, 30)
    assert cw.window_bounds('this-week', now=now) == (
        'This Week', '2026-10-12T00:00:00-04:00', '2026-10-19T00:00:00-04:00')
    assert cw.window_bounds('week', now=now) == (
        'Next 7 Days', '2026-10-16T00:00:00-04:00', '2026-10-23T00:00:00-04:00')


def test_recognizable_calendar_window_cannot_be_downgraded_by_a_router():
    text = 'Show me my calendar next week'
    job_contracts.validate_request_route(text, 'calendar', 'calendar: next-week')
    for wrong in ('calendar: week', 'calendar: this-week', 'calendar: today'):
        with pytest.raises(job_contracts.JobContractError, match='window'):
            job_contracts.validate_request_route(text, 'calendar', wrong)


@pytest.mark.parametrize('text,window,label,start,end,included,excluded', [
    ('Show me my calendar next week', 'next-week', 'Next Week', '2026-10-19', '2026-10-26',
     ('Monday class', 'Tuesday class', 'Sunday event'), ('Fair', 'Saturday workout', 'Following Monday')),
    ('Show my calendar this week', 'this-week', 'This Week', '2026-10-12', '2026-10-19',
     ('Fair', 'Saturday workout'), ('Monday class', 'Tuesday class', 'Sunday event')),
    ('Show my calendar', 'week', 'Next 7 Days', '2026-10-16', '2026-10-23',
     ('Fair', 'Saturday workout', 'Monday class', 'Tuesday class'), ('Sunday event', 'Following Monday')),
])
def test_natural_request_queries_real_calendar_connector_with_the_exact_week(
        tmp_path, monkeypatch, text, window, label, start, end, included, excluded):
    monkeypatch.setenv('WEB_SESSION_SECRET', 'calendar-week-fixture-only')
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('GOOGLE_CLIENT_ID', 'fixture-client')
    monkeypatch.setenv('GOOGLE_CLIENT_SECRET', 'fixture-client-secret')
    monkeypatch.setenv('GOOGLE_REDIRECT_URI', 'https://example.test/oauth/callback')
    connection = connectors.ConnectorStore(path=tmp_path / 'connectors.json')
    begun = connection.begin_oauth('alice')
    connection.complete_oauth('alice', begun['state'], 'fixture-code', exchange=lambda *args: {
        'access_token': 'fixture-access-only', 'expires_in': 3600,
        'scope': connectors.GOOGLE_CALENDAR_READ_SCOPE})
    monkeypatch.setattr(connectors, 'default_store', lambda: connection)
    monkeypatch.setattr(cw, 'local_now', lambda now=None: datetime(2026, 10, 16, 20, 30, tzinfo=cw.CALENDAR_TZ))
    fixtures = [
        ('Fair', '2026-10-16T00:00:00-04:00', '2026-10-17T00:00:00-04:00'),
        ('Saturday workout', '2026-10-17T00:00:00-04:00', '2026-10-18T00:00:00-04:00'),
        ('Monday class', '2026-10-19T08:30:00-04:00', '2026-10-19T09:15:00-04:00'),
        ('Tuesday class', '2026-10-20T08:30:00-04:00', '2026-10-20T09:15:00-04:00'),
        ('Sunday event', '2026-10-25T18:00:00-04:00', '2026-10-25T19:00:00-04:00'),
        ('Following Monday', '2026-10-26T00:00:00-04:00', '2026-10-26T01:00:00-04:00'),
    ]
    calls = []
    def transport(method, url, token, params=None, body=None, headers=None):
        assert method == 'GET' and body is None
        assert url == 'https://www.googleapis.com/calendar/v3/calendars/primary/events'
        assert token == 'fixture-access-only'
        calls.append(params)
        low, high = map(datetime.fromisoformat, (params['timeMin'], params['timeMax']))
        return {'items': [{'id': str(i), 'summary': title, 'start': {'dateTime': first},
                           'end': {'dateTime': last}} for i, (title, first, last) in enumerate(fixtures)
                          if datetime.fromisoformat(last) > low and datetime.fromisoformat(first) < high]}
    monkeypatch.setattr(connectors, 'default_transport', transport)
    store = CloudTaskStore(tmp_path / 'tasks.db')
    monkeypatch.setattr(task_store, 'CloudTaskStore', lambda: store)
    command = serve.natural_calendar_command(text)
    job_contracts.validate_request_route(text, 'calendar', command)
    task_id = store.submit(session_key='calendar', bot_id='calendar', rift='', chat_id='alice',
                           executor_prefix='calendar', task_text=command)
    store.claim_next('fixture-worker')
    results, sent = [], []
    ctx = SimpleNamespace(bot_owner='alice', bot_id='calendar', policy={'cal:list': 0})
    try:
        serve._run_calendar_read_task(ctx, 'alice', command, task_id,
            lambda owner, message: sent.append(message), on_result=results.append)
        assert len(calls) == 1
        assert calls[0]['timeMin'] == start + 'T00:00:00-04:00'
        assert calls[0]['timeMax'] == end + 'T00:00:00-04:00'
        assert results[0]['window'] == window
        rendered = results[0]['final_response']
        assert rendered.startswith('**' + label + '**')
        assert all(title in rendered for title in included)
        assert all(title not in rendered for title in excluded)
        assert sent == [rendered]
        assert store.complete(task_id, results[0]) == 'done'
    finally:
        store.close()
