"""Offline durable monitoring, confirmation, restart and delivery regressions."""
from datetime import datetime
from types import SimpleNamespace

import pytest

import level6_trainer_monitor as monitor
from level6_trainer_store import TrainerStore, EASTERN, CONFIRM_SECONDS, starts

NOW = datetime(2026, 10, 9, 16, tzinfo=EASTERN).timestamp()
MONDAY = '2026-10-12'
LATER = '2026-10-19'


def row(day=MONDAY, trainer='A'):
    return {'date': day, 'trainer_id': trainer, 'trainer_name': f'Coach {trainer}'}


def snapshot(*rows, occupied=None):
    return {'rows': list(rows), 'occupied_dates': occupied if occupied is not None else [r['date'] for r in rows]}


@pytest.fixture
def store(tmp_path):
    store = TrainerStore(tmp_path / 'trainers.db')
    store.configure('alice', enabled=True, delivery='group', bot_id='calendar')
    return store


def read(store, rows, when=NOW):
    with store.db() as db:
        db.execute('UPDATE monitor SET next_read=0 WHERE owner=?', ('alice',))
    cfg = store.claim('alice', now=when)
    assert cfg
    changes = store.apply_read(cfg, rows, now=when, jitter=0)
    store.release(cfg)
    return changes


def confirm(store, trainer, when, day=MONDAY):
    read(store, snapshot(row(day, trainer)), when)
    return read(store, snapshot(row(day, trainer)), when + CONFIRM_SECONDS)


def claim_delivery(store, change, when):
    with store.db() as db:
        db.execute('UPDATE monitor SET next_read=0')
    cfg = store.claim('alice', now=when)
    attempt = store.begin_delivery(cfg, change['id'], now=when)
    return cfg, attempt


def test_silent_seed_two_reads_and_restart_safe_repeated_changes(store):
    assert read(store, snapshot(row())) == []
    time = NOW + 3600
    for version, trainer in enumerate(('B', 'A', 'B'), 1):
        assert read(store, snapshot(row(trainer=trainer)), time) == []
        restarted = TrainerStore(store.path)
        changes = read(restarted, snapshot(row(trainer=trainer)), time + CONFIRM_SECONDS)
        assert len(changes) == 1 and changes[0]['version'] == version
        cfg, attempt = claim_delivery(restarted, changes[0], time + 100)
        assert attempt['attempt'] == 0
        restarted.finish_delivery(attempt, 'sent', now=time + 101)
        restarted.release(cfg)
        assert read(store, snapshot(row(trainer=trainer)), time + 200) == []
        time += 3600
    assert sorted(r['version'] for r in store.history('alice')) == [1, 2, 3]


def test_missing_disagreeing_and_failed_confirmation_preserve_baseline(store):
    read(store, snapshot(row()))
    read(store, snapshot(row(trainer='B')), NOW + 3600)
    cfg = store.claim('alice', now=NOW + 3690)
    assert cfg
    store.read_failed(cfg, 'Unavailable', now=NOW + 3690)
    store.release(cfg)
    assert read(store, snapshot(), NOW + 4000) == []
    assert read(store, snapshot(row(trainer='C')), NOW + 5000) == []
    with store.db() as db:
        base = dict(db.execute('SELECT * FROM baseline').fetchone())
    assert base['trainer_id'] == 'A' and base['version'] == 0
    changes = read(store, snapshot(row(trainer='C')), NOW + 5090)
    assert changes[0]['old_trainer_id'] == 'A' and changes[0]['trainer_id'] == 'C'


def test_later_occurrence_waits_and_coalesces_without_losing_versions(store):
    read(store, snapshot(row(), row(LATER)))
    assert confirm(store, 'B', NOW + 3600, LATER) == []
    assert confirm(store, 'C', NOW + 7200, LATER) == []
    history = store.history('alice')
    assert {r['version']: r['state'] for r in history} == {1: 'superseded', 2: 'pending'}
    after_monday = starts(MONDAY) + 60
    changes = read(store, snapshot(row(LATER, 'C')), after_monday)
    assert len(changes) == 1 and changes[0]['day'] == LATER and changes[0]['version'] == 2
    assert changes[0]['message'] == 'Your Monday class has a new trainer: Coach C.'


def test_ambiguous_nearest_slot_blocks_a_later_weekday_alert(store):
    read(store, snapshot(row(LATER)))
    confirm(store, 'B', NOW + 3600, LATER)
    assert read(store, snapshot(row(LATER, 'B'), occupied=[MONDAY, LATER]), NOW + 8000) == []
    assert store.history('alice')[0]['state'] == 'pending'


def test_reverted_candidate_never_alerts_and_name_only_change_is_silent(store):
    read(store, snapshot(row()))
    read(store, snapshot(row(trainer='B')), NOW + 3600)
    altered = {**row(), 'trainer_name': 'Coach A Updated'}
    assert read(store, snapshot(altered), NOW + 3690) == []
    assert store.history('alice') == []


def test_unknown_send_not_retried_and_manual_resend_has_a_new_attempt(store):
    read(store, snapshot(row()))
    change = confirm(store, 'B', NOW + 3600)[0]
    cfg, attempt = claim_delivery(store, change, NOW + 3800)
    assert attempt
    # A restarted worker cannot steal a live send's lease.
    assert TrainerStore(store.path).claim('alice', now=NOW + 3900) is None
    # After the lease expires, sending becomes unknown, never eligible.
    cfg2 = store.claim('alice', now=NOW + 6000)
    assert cfg2 and store.history('alice')[0]['state'] == 'unknown'
    assert store.apply_read(cfg2, snapshot(row(trainer='B')), now=NOW + 6000) == []
    store.release(cfg2)
    key = 'manual-resend-request'
    store.resend('alice', change['id'], key, now=NOW + 6100)
    assert read(store, snapshot(row(trainer='B')), NOW + 6100)[0]['attempt'] == 1
    cfg3, second = claim_delivery(store, change, NOW + 6200)
    assert second['attempt'] == 1
    store.finish_delivery(second, 'sent', now=NOW + 6201)
    store.release(cfg3)
    store.resend('alice', change['id'], key, now=NOW + 6300)
    assert store.history('alice')[0]['state'] == 'sent', 'same resend request remains idempotent after completion'
    with pytest.raises(LookupError):
        store.resend('bob', change['id'], 'different-request', now=NOW + 6300)


def test_reset_is_silent_preserves_version_and_prevents_stale_resend(store):
    read(store, snapshot(row()))
    change = confirm(store, 'B', NOW + 3600)[0]
    cfg, attempt = claim_delivery(store, change, NOW + 3800)
    store.finish_delivery(attempt, 'unknown', now=NOW + 3801)
    store.release(cfg)
    store.reset('alice')
    assert read(store, snapshot(row(trainer='C')), NOW + 4000) == []
    with pytest.raises(ValueError):
        store.resend('alice', change['id'], 'manual-resend-other', now=NOW + 4010)
    changes = confirm(store, 'D', NOW + 5000)
    assert changes[0]['version'] == 2


def test_read_in_flight_cannot_commit_after_pause_or_destination_change(store):
    cfg = store.claim('alice', now=NOW)
    store.configure('alice', enabled=False, delivery='chat', bot_id='calendar')
    assert store.apply_read(cfg, snapshot(row()), now=NOW) == []
    with store.db() as db:
        assert db.execute('SELECT count(*) FROM baseline').fetchone()[0] == 0


def test_tick_is_disabled_by_default_and_never_fetches_facebook(store, monkeypatch):
    monkeypatch.delenv('KYREX_LEVEL6_TRAINER_MONITOR_ENABLED', raising=False)
    assert monitor.enabled() is False
    sent = []
    current = [row()]
    kwargs = {'context': lambda *a: SimpleNamespace(),
        'read': lambda **kw: snapshot(*current), 'ready': lambda *a: '',
        'deliver': lambda cfg, a, ctx: (sent.append(a) or 'sent', 'Delivered'), 'jitter': 0}
    assert monitor.tick(store, now=NOW, **kwargs) == 1
    current[:] = [row(trainer='B')]
    assert monitor.tick(store, now=NOW + 3600, **kwargs) == 1
    assert sent == []
    assert monitor.tick(store, now=NOW + 3690, **kwargs) == 1
    assert len(sent) == 1
    assert monitor.tick(store, now=NOW + 3700, **kwargs) == 0
    assert len(sent) == 1


def test_chat_delivery_has_no_browser_send_and_expired_classes_never_alert(store):
    store.configure('alice', enabled=True, delivery='chat', bot_id='calendar')
    read(store, snapshot(row()))
    change = confirm(store, 'B', NOW + 3600)[0]
    cfg, attempt = claim_delivery(store, change, NOW + 3800)
    assert attempt is None and store.history('alice')[0]['state'] == 'sent'
    store.release(cfg)
    assert read(store, snapshot(row(trainer='C')), starts(MONDAY) + 1) == []
    assert len(store.history('alice')) == 1


def test_missing_confirmation_does_not_cause_a_90_second_poll_loop(store):
    read(store, snapshot(row()))
    read(store, snapshot(row(trainer='B')), NOW + 3600)
    read(store, snapshot(), NOW + 3690)
    assert store.settings('alice')['next_read'] == NOW + 3690 + 3600
    with store.db() as db:
        assert db.execute('SELECT trainer_id FROM baseline').fetchone()[0] == 'A'


def test_pending_change_uses_the_current_roster_name_without_an_extra_version(store):
    read(store, snapshot(row(), row(LATER)))
    confirm(store, 'B', NOW + 3600, LATER)
    renamed = {**row(LATER, 'B'), 'trainer_name': 'Austin Updated'}
    read(store, snapshot(renamed), NOW + 5000)
    history = store.history('alice')
    assert len(history) == 1 and history[0]['version'] == 1
    assert history[0]['message'] == 'Your Monday class has a new trainer: Austin Updated.'


def test_group_connection_wait_never_creates_a_send_attempt_or_hammers_glofox(store):
    current = [row()]
    calls = []
    kwargs = {'context': lambda *a: SimpleNamespace(),
        'read': lambda **kw: calls.append(kw) or snapshot(*current),
        'ready': lambda *a: 'Group delivery disabled', 'jitter': 0,
        'deliver': lambda *a: pytest.fail('Disabled delivery must not dispatch')}
    monitor.tick(store, now=NOW, **kwargs)
    current[:] = [row(trainer='B')]
    monitor.tick(store, now=NOW + 3600, **kwargs)
    monitor.tick(store, now=NOW + 3690, **kwargs)
    assert store.history('alice')[0]['state'] == 'eligible'
    assert store.settings('alice')['next_read'] == NOW + 3690 + 3600
    assert monitor.tick(store, now=NOW + 3800, **kwargs) == 0
    assert len(calls) == 3
    with store.db() as db:
        assert db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 0


@pytest.mark.parametrize('result,error,expected', [({'status': 'ok'}, None, 'sent'),
    ({'status': 'no_changes'}, None, 'sent'), ({'status': 'error', 'delivery_state': 'failed'}, None, 'failed'),
    ({'status': 'error'}, None, 'unknown'), (None, 'Host disconnected', 'unknown')])
def test_delivery_uses_occurrence_identity_and_the_existing_google_messages_profile(monkeypatch, result, error, expected):
    import json
    import serve
    calls = []
    monkeypatch.setattr(serve, 'browser_host_dispatch', lambda *a, **kw: calls.append((a, kw)) or (result, error))
    attempt = {'id': 'alert-id', 'day': MONDAY, 'version': 3, 'attempt': 2,
               'message': 'Your Monday class has a new trainer: Austin.'}
    state, detail = monitor.send_group({'owner': 'alice'}, attempt, SimpleNamespace())
    assert state == expected
    data = json.loads(calls[0][0][1])
    assert data['message'] == attempt['message']
    assert data['trainer_change']['version'] == 3 and data['trainer_change']['attempt'] == 2
    assert calls[0][1]['profile_bot_id'] == 'google-messages'
    assert calls[0][1]['task_id'] == 'l6-trainer-alert-id-2'
    assert 'conversation_url' not in data
