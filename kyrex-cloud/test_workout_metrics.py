"""Session-level measurements, old-phone compatibility and local-day boundaries."""
from datetime import datetime, timedelta, timezone
import pytest
from fitness_connections import FitnessConnections, FitnessError, dates, heart_rate_curve, workout_record_context

ORIGIN = 'com.sec.android.app.shealth'

@pytest.fixture
def connected(tmp_path, monkeypatch):
    monkeypatch.setenv('WEB_SESSION_SECRET', 'synthetic-workout-test-key')
    service = FitnessConnections(tmp_path / 'fitness.sqlite3')
    token = service.pair(service.begin('alice', 'samsung_health')['pairing_code'])['device_token']
    return service, token

def session():
    start = datetime.now(timezone.utc).replace(hour=12, minute=30, second=48, microsecond=0) - timedelta(days=1)
    return {'type':'workout', 'id':'workout-1', 'origin':ORIGIN,
            'start':start.isoformat(), 'end':(start+timedelta(seconds=2674)).isoformat(), 'exercise_type':0}

def sample(workout, seconds, bpm, id=None, origin=ORIGIN):
    stamp = (datetime.fromisoformat(workout['start'])+timedelta(seconds=seconds)).isoformat()
    return {'type':'heart_rate','id':id or f'hr-{seconds}', 'origin':origin,
            'start':stamp,'end':stamp,'bpm':bpm}

def read(service, workout, collection='workout'):
    day = workout['start'][:10]
    return service.read('alice', 'samsung_health', day, day, collection)['sources']['samsung_health']

def test_old_phone_samples_are_joined_to_session_without_day_or_other_owner_data(connected):
    c, token = connected; workout = session()
    c.upload(token, [workout, sample(workout,-1,250), sample(workout,0,100),
                     sample(workout,600,140), sample(workout,1200,180),
                     sample(workout,2674,250), sample(workout,600,140,'duplicate'),
                     sample(workout,100,260,'other-origin','other.health.app')])
    bob = c.pair(c.begin('bob','samsung_health')['pairing_code'])['device_token']
    c.upload(bob,[sample(workout,100,290,'bob-sample')])
    source = read(c,workout)
    assert len(source['records']) == 1
    row = source['records'][0]; metrics = row['session_metrics']
    assert metrics['duration_seconds'] == 2674
    assert metrics['heart_rate_avg_bpm'] == 140
    assert metrics['heart_rate_min_bpm'] == 100
    assert metrics['heart_rate_max_bpm'] == 180
    assert metrics['heart_rate_sample_count'] == 3
    assert metrics['heart_rate_truncated'] is False
    assert row['heart_rate_coverage']['continuity'] == 'not_verified'
    assert row['heart_rate_coverage']['largest_sample_gap_seconds'] == 600
    assert [point['average_bpm'] for point in row['heart_rate_series']] == [100,140,180]
    assert row['heart_rate_series'][1]['gap_before'] is True
    assert metrics['heart_rate_source'] == 'synced_session_samples'
    assert row['exercise_label'] == 'Other workout'
    assert 'active_calories_kcal' not in metrics
    assert row['metric_status']['active_calories'] == 'not_synced'
    assert 'No re-pair is needed' in row['sync_guidance']
    assert len(read(c,workout,'summary')['records']) == 1
    assert len(read(c,workout,'heartrate')['records']) == 7

def test_phone_aggregates_survive_encrypted_upload_and_take_precedence(connected):
    c, token = connected; workout = session()
    workout.update(title='Morning circuit', exercise_label='Strength training',
        session_metrics={'heart_rate_avg_bpm':151, 'heart_rate_min_bpm':95,
                         'heart_rate_max_bpm':179, 'heart_rate_sample_count':2300,
                         'active_calories_kcal':321.5, 'total_calories_kcal':367.2,
                         'distance_meters':1250.75, 'steps':2100},
        metric_status={'heart_rate':'ok','active_calories':'ok','total_calories':'ok',
                       'distance':'ok','steps':'ok'})
    c.upload(token,[workout,sample(workout,0,200)])
    row = read(c,workout)['records'][0]
    for key, value in workout['session_metrics'].items(): assert row['session_metrics'][key] == value
    assert row['session_metrics']['heart_rate_source'] == 'health_connect_session_aggregate'
    assert row['heart_rate_series'][0]['average_bpm'] == 200
    assert row['heart_rate_coverage']['observed_sample_count'] == 1
    assert row['title'] == 'Morning circuit'
    assert b'Morning circuit' not in c.path.read_bytes()
    c.upload(token,[workout])
    assert len(read(c,workout)['records']) == 1

def test_missing_and_zero_measurements_remain_distinct(connected):
    c,token = connected; workout = session()
    workout.update(session_metrics={'distance_meters':0},
                   metric_status={'heart_rate':'permission_missing','active_calories':'read_failed',
                                  'distance':'ok','steps':'no_data'})
    c.upload(token,[workout])
    row = read(c,workout)['records'][0]
    assert row['session_metrics']['distance_meters'] == 0
    assert 'heart_rate_avg_bpm' not in row['session_metrics']
    assert 'steps' not in row['session_metrics']
    assert row['metric_status']['heart_rate'] == 'permission_missing'
    assert row['metric_status']['active_calories'] == 'read_failed'
    assert row['metric_status']['steps'] == 'no_data'

@pytest.mark.parametrize('extras', [
    {'session_metrics':{'active_calories_kcal':float('nan')}},
    {'session_metrics':{'distance_meters':float('inf')}},
    {'session_metrics':{'steps':True}}, {'session_metrics':{'steps':1.5}},
    {'session_metrics':{'steps':10**1000}},
    {'session_metrics':{'heart_rate_avg_bpm':0}},
    {'session_metrics':{'heart_rate_avg_bpm':150,'heart_rate_max_bpm':100}},
    {'session_metrics':{'unknown_secret':'private'}},
    {'session_metrics':[]}, {'metric_status':{'heart_rate':'private'}},
    {'title':'bad\nlabel'}, {'exercise_label':'x'*121},
])
def test_invalid_workout_extras_reject_whole_batch(connected,extras):
    c,token = connected; good=session(); bad=dict(good,id='bad',**extras)
    with pytest.raises(FitnessError): c.upload(token,[good,bad])
    assert read(c,good)['records'] == []

@pytest.mark.parametrize('day,utc_start,hours', [
    ('2026-03-08','2026-03-08T05:00:00+00:00',23),
    ('2026-11-01','2026-11-01T04:00:00+00:00',25),
])
def test_local_day_includes_evening_and_observes_dst(connected,monkeypatch,day,utc_start,hours):
    c,token=connected
    start=datetime.fromisoformat(utc_start); finish=start+timedelta(hours=hours)
    frozen=finish+timedelta(hours=1)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return frozen.astimezone(tz)
    monkeypatch.setattr('fitness_connections.datetime',Clock)
    monkeypatch.setattr('fitness_connections.time.time',lambda:frozen.timestamp())
    rows=[]
    for name,stamp in [('before',start-timedelta(seconds=1)),('first',start),
                       ('last',finish-timedelta(seconds=1)),('after',finish)]:
        rows.append(dict(session(),id=name,start=stamp.isoformat(),end=stamp.isoformat()))
    c.upload(token,rows)
    result=c.read('alice','samsung_health',day,day,'workout','America/New_York')
    assert {r['id'] for r in result['sources']['samsung_health']['records']} == {'first','last'}
    assert all(r['local_start'][:10] == day for r in result['sources']['samsung_health']['records'])
    assert result['timezone'] == 'America/New_York'

def test_invalid_timezone_fails_before_network(connected):
    c,_=connected; calls=[]; c.transport=lambda *a,**k:calls.append(a)
    with pytest.raises(FitnessError,match='timezone'): c.read('alice',time_zone='invalid/zone')
    assert calls == []

def test_default_local_date_does_not_roll_over_with_utc(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return datetime(2026,10,9,0,30,tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr('fitness_connections.datetime',Clock)
    assert dates(time_zone='America/New_York') == ('2026-10-02','2026-10-08')
    assert dates(time_zone='UTC') == ('2026-10-03','2026-10-09')

def test_curve_bounds_payload_preserves_recorded_peaks_and_does_not_fill_gaps():
    values=[(second,100 if second%2 else 150) for second in range(10000) if not 4000<=second<5000]
    points,coverage=heart_rate_curve(values,10000)
    assert len(points)<=240
    assert max(point['max_bpm'] for point in points)==150
    assert not any(4000<=p['offset_seconds']<5000 for p in points)
    assert next(p for p in points if p['offset_seconds']>=5000)['gap_before']
    assert coverage['continuity']=='not_verified' and not coverage['query_capped']
    assert coverage['largest_sample_gap_seconds']==1001

def test_curve_cap_and_empty_samples_are_explicit():
    points,coverage=heart_rate_curve([],2674)
    assert points==[] and coverage['observed_sample_count']==0
    points,coverage=heart_rate_curve([(n*91,120) for n in range(1800)],172800,capped=True)
    assert len(points)==240 and coverage['curve_capped'] and coverage['query_capped']
    assert all(point['gap_before'] for point in points[1:])

def test_fresh_v05_sync_replaces_legacy_missing_statuses(connected):
    c,token=connected; workout=session()
    c.upload(token,[workout])
    assert read(c,workout)['records'][0]['metric_status']['active_calories']=='not_synced'
    workout.update(session_metrics={'active_calories_kcal':245.5},
        metric_status={'active_calories':'ok','heart_rate':'no_data','total_calories':'permission_missing',
                       'distance':'no_data','steps':'no_data'})
    c.upload(token,[workout],complete=True)
    row=read(c,workout)['records'][0]
    assert row['session_metrics']['active_calories_kcal']==245.5
    assert row['metric_status']['distance']=='no_data'
    assert 'sync_guidance' not in row

def test_report_uses_only_the_owner_read_and_omits_internal_record_identifiers(connected):
    from workout_report import build_workout_report
    import json
    c,token=connected; workout=session(); c.upload(token,[workout,sample(workout,0,120)])
    day=workout['start'][:10]
    report=build_workout_report(c.read('alice','samsung_health',day,day,'workout'))
    assert report['sessions'][0]['metrics']['heart_rate_avg_bpm']==120
    assert report['sessions'][0]['needs_sync']
    assert workout['id'] not in json.dumps(report) and ORIGIN not in json.dumps(report)
    assert build_workout_report(c.read('bob','samsung_health',day,day,'workout')) is None


def record_pair(oura_start='2026-10-09T13:20:00Z',oura_end='2026-10-09T13:40:00Z'):
    return {'sources':{
        'samsung_health':{'records':[{'type':'workout','id':'watch',
            'start':'2026-10-09T12:32:00Z','end':'2026-10-09T13:19:00Z',
            'exercise_label':'Other workout','session_metrics':{'steps':118,'heart_rate_avg_bpm':126}}]},
        'oura':{'collections':{'workout':{'records':[{'id':'ring','start_datetime':oura_start,
            'end_datetime':oura_end,'activity':'hiking','intensity':'easy'}]}}}}}


def test_adjacent_cross_source_records_are_not_merged_or_marked_duplicate():
    output=record_pair()
    FitnessConnections._mark_duplicates(output)
    context=workout_record_context(output)
    assert context['record_counts']=={'samsung_health':1,'oura':1}
    assert len(context['records'])==2
    relation=context['relationships'][0]
    assert relation['timing_status']=='non_overlapping' and relation['overlap_seconds']==0
    assert relation['gap_seconds']==60 and relation['duplicate_candidate'] is False
    assert relation['identity']=='unconfirmed'
    assert 'possible_duplicate_of' not in output['sources']['oura']['collections']['workout']['records'][0]
    watch=next(entry for entry in context['records'] if entry['source']=='samsung_health')
    assert watch['reported_activity'] is None and watch['activity_status']=='unknown'
    assert next(entry for entry in context['records'] if entry['source']=='oura')['reported_activity']=='hiking'


@pytest.mark.parametrize('start,end,overlap,candidate',[
    ('2026-10-09T12:32:00Z','2026-10-09T13:19:00Z',2820,True),
    ('2026-10-09T13:10:00Z','2026-10-09T13:30:00Z',540,False),
    ('2026-10-09T13:19:00Z','2026-10-09T13:40:00Z',0,False),
])
def test_overlap_is_a_time_fact_and_duplicate_hints_never_confirm_identity(start,end,overlap,candidate):
    output=record_pair(start,end)
    FitnessConnections._mark_duplicates(output)
    relation=workout_record_context(output)['relationships'][0]
    assert relation['overlap_seconds']==overlap and relation['duplicate_candidate'] is candidate
    assert relation['identity']=='unconfirmed'


def test_invalid_time_and_large_inventory_remain_explicitly_unknown_and_bounded():
    output=record_pair('missing','2026-10-09T13:40:00Z')
    relation=workout_record_context(output)['relationships'][0]
    assert relation['timing_status']=='unknown' and 'overlap_seconds' not in relation
    watch=output['sources']['samsung_health']['records'][0]
    output['sources']['samsung_health']['records']=[dict(watch,id=str(index)) for index in range(25)]
    context=workout_record_context(output)
    assert context['record_counts']=={'samsung_health':25,'oura':1}
    assert len(context['records'])==20 and context['omitted_records']==6


def test_owner_read_includes_inventory_without_exposing_another_owners_sessions(connected):
    c,token=connected; workout=session(); c.upload(token,[workout])
    day=workout['start'][:10]
    result=c.read('alice','samsung_health',day,day,'workout')
    assert result['workout_record_context']['record_counts']['samsung_health']==1
    other=c.read('bob','samsung_health',day,day,'workout')
    assert other['workout_record_context']['records']==[]
