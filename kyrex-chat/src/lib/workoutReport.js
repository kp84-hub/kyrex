const finite = value => typeof value === 'number' && Number.isFinite(value);
const limits = { duration_seconds: 172800, heart_rate_avg_bpm: 300, heart_rate_min_bpm: 300,
  heart_rate_max_bpm: 300, heart_rate_sample_count: 1000000, active_calories_kcal: 20000,
  total_calories_kcal: 40000, distance_meters: 1000000, steps: 400000 };
const statuses = new Set(['ok', 'not_synced', 'permission_missing', 'no_data', 'read_failed']);

export function normalizeWorkoutReport(value) {
  if (!value || value.version !== 1 || !Array.isArray(value.sessions)) return null;
  let timezone = typeof value.timezone === 'string' ? value.timezone : 'UTC';
  try { new Intl.DateTimeFormat('en-US', { timeZone: timezone }).format(); } catch { timezone = 'UTC'; }
  const sessions = value.sessions.slice(0, 10).flatMap(item => {
    if (!item || !Number.isFinite(Date.parse(item.start)) || !Number.isFinite(Date.parse(item.end))) return [];
    const duration = (Date.parse(item.end) - Date.parse(item.start)) / 1000;
    if (duration < 0 || duration > 172800) return [];
    const metrics = Object.fromEntries(Object.entries(limits).flatMap(([key, max]) => {
      const number = item.metrics?.[key];
      return finite(number) && number >= 0 && number <= max &&
        (!key.endsWith('_bpm') || number > 0) ? [[key, number]] : [];
    }));
    const series = (Array.isArray(item.heart_rate_series) ? item.heart_rate_series : []).slice(0, 240).flatMap(p => {
      if (!p || ![p.offset_seconds, p.average_bpm, p.min_bpm, p.max_bpm].every(finite) ||
          p.offset_seconds < 0 || p.offset_seconds > duration || p.min_bpm <= 0 ||
          p.max_bpm > 300 || p.min_bpm > p.average_bpm || p.average_bpm > p.max_bpm) return [];
      return [{ offset_seconds: p.offset_seconds, average_bpm: p.average_bpm,
        min_bpm: p.min_bpm, max_bpm: p.max_bpm, gap_before: p.gap_before === true }];
    }).sort((a,b) => a.offset_seconds - b.offset_seconds);
    const metricStatus = Object.fromEntries(['heart_rate','active_calories','total_calories','distance','steps']
      .map(key => [key, statuses.has(item.metric_status?.[key]) ? item.metric_status[key] : 'not_synced']));
    return [{ title: typeof item.title === 'string' ? item.title.slice(0,120) : 'Workout',
      source: item.source === 'Samsung Health' ? item.source : 'Wearable',
      start: item.start, end: item.end, duration, metrics, metricStatus, series,
      needsSync: item.needs_sync === true || Object.values(metricStatus).includes('not_synced'),
      partial: item.heart_rate_coverage?.query_capped === true || item.heart_rate_coverage?.curve_capped === true }];
  });
  return sessions.length ? { timezone, sessions, omitted: Math.max(0, Number(value.omitted_sessions) || 0),
    capped: value.record_list_capped === true } : null;
}

export const workoutMetrics = [
  { key:'duration_seconds', label:'Duration', group:null, unit:'',
    explanation:'The elapsed time between the recorded start and finish. It can include rests and pauses.' },
  { key:'heart_rate_avg_bpm', label:'Average heart rate', group:'heart_rate', unit:'bpm',
    explanation:'Your average recorded heart rate during this session. Sample-based averages describe the readings available; they do not prove continuous recording.' },
  { key:'heart_rate_max_bpm', label:'Peak heart rate', group:'heart_rate', unit:'bpm',
    explanation:'The highest heart rate reported for the session or observed in the synced samples. It is not your personal maximum heart rate or a fitness score.' },
  { key:'active_calories_kcal', label:'Active calories', group:'active_calories', unit:'kcal',
    explanation:'Samsung’s estimate of energy used through activity during this interval, excluding resting energy.' },
  { key:'total_calories_kcal', label:'Total calories', group:'total_calories', unit:'kcal',
    explanation:'Samsung’s energy estimate including both activity and resting energy. Do not add this to active calories; the totals overlap.' },
  { key:'distance_meters', label:'Distance', group:'distance', unit:'km',
    explanation:'The distance Samsung shared for this interval. Indoor and stationary workouts may have no distance measurement.' },
  { key:'steps', label:'Steps', group:'steps', unit:'',
    explanation:'Steps Samsung recorded during the workout interval. This does not count sets or repetitions.' },
];

export function metricDisplay(metric, session) {
  const value = session.metrics[metric.key];
  if (!finite(value)) return 'Unavailable';
  if (metric.key === 'duration_seconds') {
    const seconds = Math.round(value);
    return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  }
  if (metric.key === 'distance_meters') return (value / 1000).toLocaleString('en-US', { maximumFractionDigits:2 });
  return value.toLocaleString('en-US', { maximumFractionDigits: metric.key === 'steps' ? 0 : 1 });
}

export function metricMissingReason(status) {
  return ({not_synced:'This upload did not include this detail. Sync the last seven days again in Kyrex Health v0.5 after the server update.',
    permission_missing:'Allow this read permission in Kyrex Health, then sync again.',
    no_data:'Samsung did not share this measurement through Health Connect. Check Samsung’s sharing settings.',
    read_failed:'The phone could not read this detail. Retry the sync.'})[status] || '';
}

export function curveSegments(points) {
  const segments = [];
  for (const point of points) {
    const previous = segments.at(-1)?.at(-1);
    // Bucket centers can be far apart in long sessions. Only the host's raw
    // sample gap flag distinguishes missing readings from downsampling.
    if (!previous || point.gap_before)
      segments.push([]);
    segments.at(-1).push(point);
  }
  return segments;
}
