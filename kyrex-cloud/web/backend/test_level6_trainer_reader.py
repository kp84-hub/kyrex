"""The monitor reads a bounded rolling Glofox horizon, including Sundays."""
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import pytest

import glofox_api as api
from level6_trainer_store import EASTERN


def event(day, trainer='A', hour=8):
    return {'_id': f'{day}-{hour}-{trainer}', 'name': api.SCHEDULED_CLASS_NAME,
        'time_start': int(datetime.fromisoformat(day).replace(hour=hour, minute=30, tzinfo=EASTERN).timestamp()),
        'type': 'event', 'duration': 45, 'branch_id': api.BRANCH_ID, 'trainers': [trainer],
        'trainers_obj': [{'_id': trainer, 'branch_id': api.BRANCH_ID, 'first_name': 'Coach', 'last_name': trainer, 'name': f'Coach {trainer}'}]}


@pytest.fixture
def reader(monkeypatch):
    records, urls = [], []
    monkeypatch.setattr(api, '_post_guest_login', lambda: 'PRIVATE-TOKEN')
    monkeypatch.setattr(api, 'get_branch', lambda token: {})
    monkeypatch.setattr(api, 'get_trainers', lambda token: {'A': 'Coach A', 'B': 'Coach B'})
    def get(url, token):
        urls.append(url)
        assert token == 'PRIVATE-TOKEN'
        return list(records), len(records), False
    monkeypatch.setattr(api, '_get_json_list', get)
    return records, urls


def test_rolls_across_weeks_and_reads_sunday(reader):
    records, urls = reader
    records.extend([event('2026-10-11'), event('2026-10-12'), event('2026-10-18', 'B')])
    result = api.upcoming_0830_classes(now=datetime(2026, 10, 9, 12, tzinfo=EASTERN))
    assert [r['date'] for r in result['rows']] == ['2026-10-11', '2026-10-12', '2026-10-18']
    assert '2026-10-11' not in result['unavailable_dates']
    query = parse_qs(urlsplit(urls[0]).query)
    assert int(query['start'][0]) == int(datetime(2026, 10, 9, tzinfo=EASTERN).timestamp())
    assert int(query['end'][0]) == int(datetime(2026, 10, 22, 23, 59, 59, tzinfo=EASTERN).timestamp())
    assert 'filter' not in query and query['include'] == ['trainers,facility,program,users_booked']
    assert 'PRIVATE-TOKEN' not in str(result)


def test_absent_sunday_is_normal_and_invalid_slot_does_not_poison_other_days(reader):
    records, _ = reader
    bad = event('2026-10-12')
    bad['trainers'] = []
    records.extend([bad, event('2026-10-13'), event('2026-10-14'), event('2026-10-14', 'B')])
    result = api.upcoming_0830_classes(now=datetime(2026, 10, 9, tzinfo=EASTERN))
    assert [r['date'] for r in result['rows']] == ['2026-10-13']
    assert {'2026-10-11', '2026-10-12', '2026-10-14'} <= set(result['unavailable_dates'])
    assert result['occupied_dates'] == ['2026-10-12', '2026-10-13', '2026-10-14']


def test_dst_window_uses_local_calendar_dates(reader):
    _, urls = reader
    api.upcoming_0830_classes(now=datetime(2026, 10, 30, tzinfo=EASTERN))
    query = parse_qs(urlsplit(urls[0]).query)
    assert int(query['end'][0]) - int(query['start'][0]) == (14 * 86400 + 3600 - 1)


@pytest.mark.parametrize('bad', [{}, {'time_start': 'bad'}, event('2026-11-01')])
def test_unattributable_or_out_of_window_data_fails_the_read(reader, bad):
    reader[0].append(bad)
    with pytest.raises(api.GlofoxSchemaError):
        api.upcoming_0830_classes(now=datetime(2026, 10, 9, tzinfo=EASTERN))


def test_duplicate_ids_fail_closed(reader):
    reader[0].extend([event('2026-10-12')] * 2)
    with pytest.raises(api.GlofoxDataError):
        api.upcoming_0830_classes(now=datetime(2026, 10, 9, tzinfo=EASTERN))


def test_untrusted_trainer_text_never_enters_an_alert(reader, monkeypatch):
    reader[0].append(event('2026-10-12'))
    name = 'Coach\nhttps://bad.example'
    reader[0][0]['trainers_obj'][0]['name'] = name
    monkeypatch.setattr(api, 'get_trainers', lambda token: {'A': name})
    result = api.upcoming_0830_classes(now=datetime(2026, 10, 9, tzinfo=EASTERN))
    assert result['rows'] == [] and '2026-10-12' in result['unavailable_dates']
