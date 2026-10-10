const finite = value => typeof value === 'number' && Number.isFinite(value);
const dateKey = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) &&
  Number.isFinite(Date.parse(`${value}T00:00:00Z`)) && new Date(`${value}T00:00:00Z`).toISOString().slice(0,10) === value;

export const sleepMetrics = [
  {key:'total_sleep_seconds', label:'Time asleep', unit:'h', max:172800,
    explanation:'Estimated time asleep in the selected session. Oura estimates it from ring data; Samsung reports the synced sleep-stage total. Extra sessions and naps are not added to this view.'},
  {key:'time_in_bed_seconds', label:'Time in bed', unit:'h', max:172800,
    explanation:'Time between the recorded session start and end, including awake time. This interval is not the same as time asleep.'},
  {key:'efficiency_pct', label:'Efficiency', unit:'%', max:100,
    explanation:'Oura’s estimated percentage of the sleep interval spent asleep. It is not a diagnosis or proof that sleep was restorative. Samsung does not share this metric in the current sync.'},
  {key:'heart_rate_bpm', label:'Sleeping heart rate', unit:'bpm', max:300,
    explanation:'Oura’s average heart rate during the selected sleep session. Compare it with your own recorded nights; this value alone cannot establish recovery or a health condition.'},
  {key:'hrv_ms', label:'HRV', unit:'ms', max:1000,
    explanation:'Oura’s average heart rate variability during the selected sleep session, in milliseconds. It describes variation between heartbeats. Interpret it alongside your personal history, not a universal score.'},
];

export function normalizeSleepReport(value) {
  if (!value || value.version !== 1 || !dateKey(value.start_date) || !dateKey(value.end_date) || !Array.isArray(value.sources)) return null;
  const first = Date.parse(`${value.start_date}T00:00:00Z`), last = Date.parse(`${value.end_date}T00:00:00Z`);
  const count = (last-first)/86400000 + 1;
  if (count < 1 || count > 31 || !Number.isInteger(count)) return null;
  let timezone = typeof value.timezone === 'string' ? value.timezone : 'UTC';
  try { new Intl.DateTimeFormat('en-US',{timeZone:timezone}).format(); } catch { timezone = 'UTC'; }
  const seen = new Set();
  const sources = value.sources.slice(0,2).flatMap(source => {
    if (!source || !['oura','samsung_health'].includes(source.id) || seen.has(source.id)) return [];
    seen.add(source.id);
    const rows = new Map((['ok','connected'].includes(source.status) && Array.isArray(source.nights) ? source.nights : []).slice(0,31)
      .filter(night => night && dateKey(night.date)).map(night => [night.date,night]));
    const nights = Array.from({length:count},(_,index) => {
      const date = new Date(first+index*86400000).toISOString().slice(0,10);
      const row = rows.get(date) || {};
      const interval = (Date.parse(row.end)-Date.parse(row.start))/1000;
      const valid = Number.isFinite(interval) && interval > 0 && interval <= 172800 &&
        /(?:Z|[+-]\d{2}:\d{2})$/.test(row.start || '') && /(?:Z|[+-]\d{2}:\d{2})$/.test(row.end || '');
      const metrics = Object.fromEntries(sleepMetrics.flatMap(metric => {
        const number = row.metrics?.[metric.key];
        return valid && finite(number) && number >= 0 && number <= metric.max &&
          (!metric.key.endsWith('_seconds') || number <= interval) &&
          (metric.key !== 'heart_rate_bpm' || number > 0) ? [[metric.key,number]] : [];
      }));
      return {date,metrics,start:valid ? row.start : null,end:valid ? row.end : null,
        selection: valid && row.selection === 'main' ? 'main' : valid ? 'longest' : 'none',
        otherSessions: Number.isInteger(row.other_sessions) ? Math.max(0,Math.min(2000,row.other_sessions)) : 0};
    });
    return [{id:source.id,label:source.id === 'oura' ? 'Oura' : 'Samsung Health',nights,
      status:['ok','connected','failed','permission_missing'].includes(source.status) ? source.status : 'unavailable',
      incomplete:source.incomplete === true,capped:source.capped === true}];
  });
  return sources.length ? {timezone,sources,start:value.start_date,end:value.end_date} : null;
}

export function sleepValue(value, metric) {
  if (!finite(value)) return 'Unavailable';
  if (metric.key.endsWith('_seconds')) {
    const minutes = Math.round(value/60);
    return `${Math.floor(minutes/60)}h ${String(minutes%60).padStart(2,'0')}m`;
  }
  return `${value.toLocaleString('en-US',{maximumFractionDigits:1})} ${metric.unit}`;
}

export function sleepAverage(nights, metric) {
  const values = nights.map(night => night.metrics[metric.key]).filter(finite);
  return {count:values.length,value:values.length ? values.reduce((sum,n) => sum+n,0)/values.length : null};
}
