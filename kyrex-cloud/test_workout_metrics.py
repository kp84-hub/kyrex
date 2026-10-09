"""Session-level measurements, old-phone compatibility and local-day boundaries."""
from datetime import datetime, timedelta, timezone
import pytest
from fitness_connections import FitnessConnections, FitnessError, dates

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
    assert metrics['heart_rate_source'] == 'synced_session_samples'
    assert row['exercise_label'] == 'Other workout'
    assert 'active_calories_kcal' not in metrics
    assert row['metric_status']['active_calories'] == 'not_synced'
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
