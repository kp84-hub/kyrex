import React, { useEffect, useId, useRef, useState } from 'react';
import { normalizeWorkoutReport, workoutMetrics, metricDisplay, metricMissingReason, curveSegments } from '../lib/workoutReport.js';

function HeartRateChart({ session }) {
  const id = useId().replace(/[^a-zA-Z0-9_-]/g, '');
  const [selected, setSelected] = useState(null);
  const chartRef = useRef(null);
  const [width, setWidth] = useState(680);
  const points = session.series;
  useEffect(() => {
    if (!chartRef.current || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(entries => {
      const measured = entries[0]?.contentRect?.width;
      if (measured > 0) setWidth(Math.max(280, measured));
    });
    observer.observe(chartRef.current);
    return () => observer.disconnect();
  }, [points.length > 0]);
  if (!points.length) return <div className="workout-chart-empty">No synced heart-rate timeline is available for this session. Summary values alone cannot create a trend.</div>;
  const low = Math.max(0, Math.floor(Math.min(...points.map(p => p.min_bpm)) / 10) * 10 - 10);
  const high = Math.ceil(Math.max(...points.map(p => p.max_bpm)) / 10) * 10 + 10;
  const plotWidth = width - 86;
  const x = p => 48 + p.offset_seconds / Math.max(1, session.duration) * plotWidth;
  const y = value => 210 - (value - low) / Math.max(1, high - low) * 170;
  const segments = curveSegments(points);
  const active = points[Math.min(selected ?? Math.floor(points.length / 2), points.length-1)];
  const elapsed = Math.round(active.offset_seconds);
  const path = segment => segment.map((p,i) => `${i ? 'L' : 'M'}${x(p).toFixed(2)},${y(p.average_bpm).toFixed(2)}`).join(' ');
  const band = segment => `${segment.map((p,i) => `${i ? 'L' : 'M'}${x(p)},${y(p.max_bpm)}`).join(' ')} ${[...segment].reverse().map(p => `L${x(p)},${y(p.min_bpm)}`).join(' ')} Z`;
  const choose = event => {
    const rect = event.currentTarget.getBoundingClientRect();
    if (!rect.width) return;
    const position = (event.clientX - rect.left) / rect.width * width;
    let nearest = 0;
    points.forEach((p,i) => { if (Math.abs(x(p)-position) < Math.abs(x(points[nearest])-position)) nearest=i; });
    setSelected(nearest);
  };
  return <div className="workout-curve" ref={chartRef}>
    <div className="workout-chart-heading"><strong>Heart rate through your workout</strong><span>bpm</span></div>
    <svg viewBox={`0 0 ${width} 250`} role="img" aria-label="Observed heart rate over elapsed workout time. Blank sections indicate sampling gaps."
      tabIndex={0} onPointerMove={choose} onPointerDown={choose}
      onKeyDown={e => {
        if (!['ArrowLeft','ArrowRight'].includes(e.key)) return;
        e.preventDefault(); setSelected(Math.max(0,Math.min(points.length-1,(selected ?? 0)+(e.key === 'ArrowRight' ? 1 : -1))));
      }}>
      <title>Samsung Health observed heart rate; nearby sample means and recorded ranges</title>
      <defs><linearGradient id={`${id}-line`} x1="0" x2="1"><stop offset="0%" stopColor="#66e7d5"/><stop offset="100%" stopColor="#ac9aff"/></linearGradient></defs>
      {[low,(low+high)/2,high].map(value => <g key={value}>
        <line x1="48" x2={width-38} y1={y(value)} y2={y(value)} stroke="currentColor" className="workout-grid-line"/>
        <text x="39" y={y(value)+4} textAnchor="end">{Math.round(value)}</text>
      </g>)}
      {(width < 450 ? [0,.5,1] : [0,.25,.5,.75,1]).map(fraction => <text key={fraction} x={48+plotWidth*fraction} y="237" textAnchor="middle">{Math.round(session.duration*fraction/60)}m</text>)}
      {segments.map((segment,i) => <g key={i}>
        <path d={band(segment)} fill="#8b9aff" opacity=".15"/>
        <path d={path(segment)} fill="none" stroke={`url(#${id}-line)`} strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"/>
        {segment.length === 1 ? <circle cx={x(segment[0])} cy={y(segment[0].average_bpm)} r="3" fill="#66e7d5"/> : null}
      </g>)}
      {selected !== null ? <g>
        <line x1={x(active)} x2={x(active)} y1="40" y2="210" stroke="#dbe3ef" strokeDasharray="3 5" opacity=".5"/>
        <circle cx={x(active)} cy={y(active.average_bpm)} r="5" fill="#0e1520" stroke="#66e7d5" strokeWidth="3"/>
      </g> : null}
    </svg>
    <div className="workout-chart-reading" aria-live="polite">
      <span>{Math.floor(elapsed/60)}m {elapsed%60}s into the session</span>
      <strong>{active.average_bpm} bpm</strong><span>Recorded range {active.min_bpm}–{active.max_bpm}</span>
    </div>
    <p className="workout-chart-note">Tap the curve or use the arrow keys. The line shows sample averages; shading shows their recorded range. Gaps over 90 seconds stay blank. {session.partial ? 'Only part of the timeline is shown. ' : ''}Recording continuity is not verified.</p>
  </div>;
}

function WorkoutSession({ session, timezone }) {
  const [selectedMetric, setSelectedMetric] = useState('heart_rate_avg_bpm');
  const selected = workoutMetrics.find(metric => metric.key === selectedMetric);
  const time = date => new Intl.DateTimeFormat('en-US', {timeZone:timezone, hour:'numeric', minute:'2-digit'}).format(new Date(date));
  const date = new Intl.DateTimeFormat('en-US', {timeZone:timezone, weekday:'short', month:'short', day:'numeric'}).format(new Date(session.start));
  const selectedValue = session.metrics[selected.key];
  return <section className="workout-session" aria-label={`${date} workout report`}>
    <div className="workout-card-header"><div><span className="workout-eyebrow">YOUR WORKOUT · {session.source}</span><h3>{session.title}</h3><p>{date} · {time(session.start)}–{time(session.end)} · {timezone.replace(/_/g,' ')}</p></div><span className="workout-card-icon" aria-hidden="true">↗</span></div>
    <HeartRateChart session={session}/>
    <div className="workout-metric-grid">
      {workoutMetrics.map(metric => {
        const available = Number.isFinite(session.metrics[metric.key]);
        return <button key={metric.key} type="button" className={`workout-metric ${selectedMetric === metric.key ? 'selected' : ''}`}
          aria-pressed={selectedMetric === metric.key} onClick={() => setSelectedMetric(metric.key)}>
          <span>{metric.label}</span><strong className={available ? '' : 'unavailable'}>{metricDisplay(metric,session)}{available && metric.unit ? <small> {metric.unit}</small> : null}</strong>
          {!available ? <small>{({not_synced:'Sync needed',permission_missing:'Access needed',no_data:'Not shared',read_failed:'Read failed'})[session.metricStatus[metric.group]] || 'Not shared'}</small> : null}
        </button>;
      })}
    </div>
    <div className="workout-metric-explanation" role="status"><strong>{selected.label}</strong><p>{selected.explanation}</p>
      {!Number.isFinite(selectedValue) ? <p>{metricMissingReason(session.metricStatus[selected.group])}</p> : null}
    </div>
    {session.needsSync ? <div className="workout-sync-note"><strong>Refresh workout details</strong><p>In Kyrex Health v0.5, allow the desired Health Connect access, then tap <b>Sync last 7 days</b> after the server update. No re-pair is needed. Calories and distance will appear if Samsung shares them.</p></div> : null}
  </section>;
}

export default function WorkoutReport({ report }) {
  const normalized = normalizeWorkoutReport(report);
  if (!normalized) return null;
  return <div className="workout-report">{normalized.sessions.map((session,index) => <WorkoutSession key={`${session.start}-${index}`} session={session} timezone={normalized.timezone}/>)}
    {normalized.omitted || normalized.capped ? <p className="workout-chart-note">Showing up to ten sessions. Ask for a smaller date range to see other workouts.</p> : null}
  </div>;
}
