from datetime import datetime, timezone
import json
import pytest
import sleep_report as sleep


def snapshot(rows, samsung=None):
    result = {'start_date':'2026-10-03','end_date':'2026-10-05','timezone':'America/New_York',
        'sources':{'oura':{'status':'connected','collections':{'sleep':{'status':'ok','records':rows}}}}}
    if samsung is not None:
        result['sources']['samsung_health']={'status':'connected','records':samsung,'incomplete':True}
    return result


def record(day='2026-10-03', **kwargs):
    return {'id':'private-id','day':day,'bedtime_start':day+'T00:00:00-04:00',
        'bedtime_end':day+'T08:00:00-04:00','total_sleep_duration':25200,
        'type':'long_sleep','efficiency':90,'average_heart_rate':52,'average_hrv':45, **kwargs}


def test_main_naps_missing_dates_sources_never_added():
    result=sleep.build_sleep_report(snapshot([record(),record(type='sleep',total_sleep_duration=1000)],
        [{'type':'sleep','start':'2026-10-03T02:00:00Z','end':'2026-10-03T12:00:00Z','duration_seconds':26000}]))
    assert len(result['sources'])==2
    oura,samsung=result['sources']
    assert oura['nights'][0]['selection']=='main'
    assert oura['nights'][0]['metrics']['total_sleep_seconds']==25200
    assert oura['nights'][0]['metrics']['time_in_bed_seconds']==28800
    assert oura['nights'][0]['other_sessions']==1
    assert oura['nights'][1]['metrics']=={}
    assert samsung['nights'][0]['metrics']['total_sleep_seconds']==26000
    assert 'efficiency_pct' not in samsung['nights'][0]['metrics']
    assert samsung['incomplete']
    assert 'private-id' not in json.dumps(result)


def test_longest_unknown_session_not_claimed_main_and_naps_only_not_a_night():
    rows=[record(type=None,total_sleep_duration=18000),record(type=None,total_sleep_duration=25000),
          record('2026-10-04',type='sleep',total_sleep_duration=1800)]
    result=sleep.build_sleep_report(snapshot(rows))['sources'][0]['nights']
    assert result[0]['selection']=='longest' and result[0]['metrics']['total_sleep_seconds']==25000
    assert result[1]['metrics']=={} and result[1]['other_sessions']==1


@pytest.mark.parametrize('value',[True,-1,float('inf'),500000,'eight hours'])
def test_invalid_duration_cannot_become_asleep(value):
    night=sleep.build_sleep_report(snapshot([record(total_sleep_duration=value)]))['sources'][0]['nights'][0]
    assert 'total_sleep_seconds' not in night['metrics']
    assert night['metrics']['time_in_bed_seconds']==28800


def test_sleep_duration_cannot_exceed_recorded_interval_and_naive_times_are_skipped():
    result=sleep.build_sleep_report(snapshot([record(total_sleep_duration=30000),
        record('2026-10-04',bedtime_start='2026-10-04T00:00:00',bedtime_end='2026-10-04T08:00:00')]))
    source=result['sources'][0]
    assert 'total_sleep_seconds' not in source['nights'][0]['metrics']
    assert source['nights'][1]['metrics']=={} and source['incomplete']


def test_date_and_status_bounds():
    value=snapshot([]); value['end_date']='2026-12-01'
    assert sleep.build_sleep_report(value) is None
    value=snapshot([record()]); value['sources']['oura']['collections']['sleep']['status']='failed'
    report=sleep.build_sleep_report(value)
    assert not any(n['metrics'] for n in report['sources'][0]['nights'])
    assert report['sources'][0]['status']=='failed'
    assert sleep.build_sleep_report({'sources':{}}) is None


def test_graph_requests_have_explicit_local_dates_and_do_not_narrow_other_ranges(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return datetime(2026,10,10,2,0,tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(sleep,'datetime',Clock)
    assert sleep.sleep_graph_request('Graph out my sleep data')['start']=='2026-10-03'
    assert sleep.sleep_graph_request('Graph out my sleep data')['end']=='2026-10-09'
    assert sleep.sleep_graph_request('Graph last night’s sleep')['start']=='2026-10-09'
    assert sleep.sleep_graph_request('Chart sleep for 2026-10-01 to 2026-10-08')['end']=='2026-10-08'
    for text in ('Chart sleep last month','Graph sleep this week','Graph sleep for the last 30 nights',
                 'Graph sleep today and last night','Graph sleep and workouts','Plot sleep in Europe/London','Graph sleep 2026-02-30','Graph my workout','How was my sleep?'):
        assert sleep.sleep_graph_request(text) is None
