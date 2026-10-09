"""Real fixed-group send path with fake controls: no network or live messages."""
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import browser_operator as operator
import browser_host_channel as channel
import serve
from level6_trainer_store import EASTERN, alert_text


def spec(day=None, version=1, attempt=0):
    day = day or (datetime.now(EASTERN).date() + timedelta(days=2)).isoformat()
    return {'google_messages_level6': True, 'url': operator.GOOGLE_MESSAGES_BASE_URL,
        'message': alert_text(day, 'Austin'),
        'trainer_change': {'date': day, 'version': version, 'attempt': attempt, 'owner_key': 'a' * 16}}


class Control:
    def __init__(self, composer=None):
        self.value, self.clicks, self.composer, self.fail_click = '', 0, composer, False
    def is_visible(self):
        return True
    def fill(self, value, **kw):
        self.value = value
    def evaluate(self, code):
        return True
    def input_value(self):
        return self.value
    def click(self, **kw):
        self.clicks += 1
        self.composer.value = ''
        if self.fail_click:
            raise TimeoutError('click finished but response lost')


class Locator:
    def __init__(self, control=None):
        self.control = control
    def count(self):
        return int(self.control is not None)
    def nth(self, index):
        return self.control


@pytest.fixture
def sender(tmp_path, monkeypatch):
    composer = Control()
    send = Control(composer)
    def locator(selector):
        if selector == '[contenteditable="true"][role="textbox"]':
            return Locator(composer)
        if selector == 'button[aria-label*="send" i]':
            return Locator(send)
        return Locator()
    driver = SimpleNamespace(session_dir=tmp_path, _page=SimpleNamespace(locator=locator, wait_for_timeout=lambda n: None),
        navigate=lambda url: None, current_url=lambda: 'https://messages.google.com/web/conversations/fixed-group')
    proto = SimpleNamespace(operation=lambda *a: True, progress=lambda *a: None, redact=lambda s: s)
    monkeypatch.setenv('KYREX_GOOGLE_MESSAGES_CONVERSATION_URL', driver.current_url())
    def run(data):
        action = operator.parse_spec(json.dumps(data))[0]
        return operator._run_google_messages_level6(driver, proto, action, root=tmp_path, allowlist=['messages.google.com'])
    return run, send, composer, tmp_path


def test_receipts_use_occurrence_version_and_manual_attempt_instead_of_text(sender):
    run, button, composer, path = sender
    first = spec()
    assert run(first)['status'] == 'ok' and button.clicks == 1
    assert run(first)['status'] == 'no_changes' and button.clicks == 1
    assert run(spec(first['trainer_change']['date'], version=2))['status'] == 'ok'
    assert run(spec(first['trainer_change']['date'], version=2, attempt=1))['status'] == 'ok'
    later = (datetime.fromisoformat(first['trainer_change']['date']).date() + timedelta(days=7)).isoformat()
    assert spec(later)['message'] == first['message']
    assert run(spec(later))['status'] == 'ok' and button.clicks == 4
    receipts = json.loads((path / '.kyrex-level6-message-receipts.json').read_text())
    assert len(set(receipts)) == 4


@pytest.mark.parametrize('mutation', [lambda s: s.update(message='hello'),
    lambda s: s.update(message=s['message'] + '\nMore information'),
    lambda s: s.update(message=s['message'] + ' https://example.com'),
    lambda s: s['trainer_change'].update(version=True),
    lambda s: s['trainer_change'].update(attempt=-1),
    lambda s: s['trainer_change'].update(owner_key='another-owner'),
    lambda s: s.update(trainer_change=None),
    lambda s: s.update(destination='some other group')])
def test_alert_gate_rejects_unbounded_or_unrelated_payloads(mutation):
    data = spec()
    mutation(data)
    with pytest.raises(operator.SpecError):
        operator.parse_spec(json.dumps(data))


def test_post_click_timeout_is_unknown_not_a_retryable_failed_send(sender):
    run, button, _, _ = sender
    button.fail_click = True
    result = run(spec())
    assert result['status'] == 'error' and result['delivery_state'] == 'unknown'
    assert button.clicks == 1


def test_receipt_write_failure_after_send_is_unknown(sender, monkeypatch):
    run, button, _, path = sender
    monkeypatch.setattr(operator.os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    result = run(spec())
    assert result['delivery_state'] == 'unknown' and button.clicks == 1
    assert not (path / '.kyrex-level6-message-receipts.json').exists()


def test_unpaired_and_expired_classes_fail_before_click(sender, monkeypatch):
    run, button, _, _ = sender
    result = run(spec('2000-01-03'))
    assert result['delivery_state'] == 'failed' and button.clicks == 0
    monkeypatch.delenv('KYREX_GOOGLE_MESSAGES_CONVERSATION_URL')
    assert run(spec())['delivery_state'] == 'failed' and button.clicks == 0


def test_cloud_profile_gate_uses_the_shared_alert_parser(monkeypatch):
    calls = []
    ctx = SimpleNamespace(bot_owner='alice', browser_allowlist=[], policy=serve.CALENDAR_PRESET)
    monkeypatch.setattr(serve, 'build_context', lambda bid: ctx)
    monkeypatch.setattr(channel._hosts, 'host_for', lambda *a, **kw: SimpleNamespace(host_id='host', effective_state=lambda: 'online'))
    manager = channel.HostManager()
    live = SimpleNamespace(authenticated=True, dispatch_task=lambda **kw: calls.append(kw) or {'status': 'ok'})
    monkeypatch.setattr(manager, 'channel_for', lambda host: live)
    result = manager.dispatch_browser_task('alice', 'calendar', json.dumps(spec()), profile_bot_id=serve.LEVEL6_MESSAGES_PROFILE_ID)
    assert result['status'] == 'ok'
    assert calls[0]['bot_id'] == 'google-messages' and calls[0]['allowlist'] == ['messages.google.com']
    with pytest.raises(channel.ChannelError):
        manager.dispatch_browser_task('bob', 'calendar', json.dumps(spec()), profile_bot_id=serve.LEVEL6_MESSAGES_PROFILE_ID)


def test_owner_draft_is_never_replaced_by_an_automatic_alert(sender):
    run, button, composer, _ = sender
    composer.value = 'Owner’s unsent message'
    result = run(spec())
    assert result['delivery_state'] == 'failed'
    assert composer.value == 'Owner’s unsent message' and button.clicks == 0
