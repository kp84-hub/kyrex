"""Native Chat cards built exclusively from the owner's fitness tool response."""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def workout_day(text, timezone='America/New_York'):
    """Resolve a single relative workout day from the current owner request."""
    if not re.search(r'\bworkouts?\b', text, re.I): return None
    # Comparisons and explicit dates must keep the owner's requested range.
    if re.search(r'\b\d{4}-\d{2}-\d{2}\b|\b(compare|week|month|last|past|versus|vs)\b', text, re.I):
        return None
    days = set(re.findall(r'\b(today|yesterday|tomorrow)\b', text, re.I))
    days = {day.lower() for day in days}
    if len(days) != 1: return None
    offset = {'today':0, 'yesterday':-1, 'tomorrow':1}[days.pop()]
    try:
        return (datetime.now(ZoneInfo(timezone)).date()+timedelta(days=offset)).isoformat()
    except (ValueError, ZoneInfoNotFoundError):
        return None  # The fitness store returns its normal timezone error.


def workout_graph_request(text):
    """A chart or coaching request gets fresh readings and the current profile."""
    if not (re.search(r'\bworkouts?\b', text, re.I) and
            re.search(r'\b(graph|chart|plot|visuali[sz]e|review|evaluate|assess|analy[sz]e|coach|coaching|feedback|improve)\b', text, re.I)):
        return None
    frame = {'provider':'all', 'collection':'workout', 'timezone':'America/New_York'}
    day = workout_day(text)
    explicit = re.findall(r'\b\d{4}-\d{2}-\d{2}\b', text)
    if day:
        frame.update(start=day, end=day)
    elif len(explicit) in (1,2):
        try:
            for value in explicit: date.fromisoformat(value)
        except ValueError:
            return None
        frame.update(start=explicit[0], end=explicit[-1])
    elif explicit:
        return None
    # Complex relative ranges remain model-driven rather than being narrowed.
    elif len({day.lower() for day in re.findall(r'\b(today|yesterday|tomorrow)\b',text,re.I)}) > 1:
        return None
    elif re.search(r'\b(compare|week|month|last|past|versus|vs|tomorrow)\b', text, re.I):
        return None
    return frame

def build_workout_report(result):
    source = result.get('sources', {}).get('samsung_health', {})
    if source.get('status') != 'connected': return None
    rows = [row for row in source.get('records', []) if row.get('type') == 'workout']
    if not rows: return None
    sessions = []
    fields = ('duration_seconds','heart_rate_avg_bpm','heart_rate_min_bpm','heart_rate_max_bpm',
              'heart_rate_sample_count','heart_rate_source','active_calories_kcal',
              'total_calories_kcal','distance_meters','steps')
    for row in rows[:10]:
        metrics = row.get('session_metrics', {})
        sessions.append({'title':row.get('title') or row.get('exercise_label') or 'Workout',
            'source':'Samsung Health', 'start':row['start'], 'end':row['end'],
            'metrics':{key:metrics[key] for key in fields if key in metrics},
            'metric_status':row.get('metric_status', {}),
            'heart_rate_series':row.get('heart_rate_series', [])[:240],
            'heart_rate_coverage':row.get('heart_rate_coverage', {}),
            'needs_sync':any(value == 'not_synced' for value in row.get('metric_status', {}).values())})
    return {'version':1, 'timezone':result.get('timezone','America/New_York'),
            'synced_at':source.get('synced_at'), 'sessions':sessions,
            'omitted_sessions':max(0,len(rows)-10), 'record_list_capped':bool(source.get('truncated'))}
