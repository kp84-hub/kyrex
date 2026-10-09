"""Fresh chart requests and their owner-specified local dates."""
from datetime import datetime,timezone
import pytest
import workout_report

@pytest.fixture
def local_evening(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return datetime(2026,10,9,0,30,tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(workout_report,'datetime',Clock)

def test_relative_day_uses_local_date_and_leaves_comparisons_and_explicit_dates(local_evening):
    assert workout_report.workout_day("Graph today’s workout")=='2026-10-08'
    assert workout_report.workout_day("Graph yesterday's workout")=='2026-10-07'
    assert workout_report.workout_day('Pull my workout today', 'UTC')=='2026-10-09'
    assert workout_report.workout_day('Compare workouts today and yesterday') is None
    assert workout_report.workout_day('Graph my workout for 2026-10-02 instead of today') is None
    assert workout_report.workout_day('What was my heart rate today?') is None
    assert workout_report.workout_day('Pull my workout today','invalid/zone') is None

def test_graph_requests_have_fresh_bounded_dates(local_evening):
    assert workout_report.workout_graph_request("Graph today's workout")['start']=='2026-10-08'
    assert workout_report.workout_graph_request('Chart my workout on 2026-10-02')['end']=='2026-10-02'
    frame=workout_report.workout_graph_request('Plot workouts from 2026-10-01 to 2026-10-08')
    assert frame['start']=='2026-10-01' and frame['end']=='2026-10-08'
    assert 'start' not in workout_report.workout_graph_request('Graph my workouts')
    assert workout_report.workout_graph_request('Graph my workout on 2026-02-30') is None
    assert workout_report.workout_graph_request('Compare workouts over the past month in a chart') is None
    assert workout_report.workout_graph_request('Graph workouts today and yesterday') is None
    assert workout_report.workout_graph_request('Explain my workout') is None
    assert workout_report.workout_graph_request('Graph my calendar') is None
