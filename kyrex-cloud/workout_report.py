"""Native Chat cards built exclusively from the owner's fitness tool response."""

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
