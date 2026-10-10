import React, {useId,useState} from 'react';
import {normalizeSleepReport,sleepMetrics,sleepValue,sleepAverage} from '../lib/sleepReport.js';

const label = date => new Intl.DateTimeFormat('en-US',{timeZone:'UTC',month:'short',day:'numeric'}).format(new Date(`${date}T00:00:00Z`));

function SleepCard({report}) {
  const id = useId().replace(/[^a-zA-Z0-9_-]/g,'');
  const initial = report.sources.find(source => source.nights.some(night => Object.keys(night.metrics).length)) || report.sources[0];
  const latest = source => Math.max(0,source.nights.findLastIndex(night => Object.keys(night.metrics).length));
  const [sourceId,setSource] = useState(initial.id);
  const [metricKey,setMetric] = useState('total_sleep_seconds');
  const [selected,setSelected] = useState(latest(initial));
  const source = report.sources.find(item => item.id === sourceId) || report.sources[0];
  const metric = sleepMetrics.find(item => item.key === metricKey);
  const nights = source.nights;
  const active = nights[Math.min(selected,nights.length-1)];
  const average = sleepAverage(nights,metric);
  const toChart = value => metric.key.endsWith('_seconds') ? value/3600 : value;
  const maximum = Math.max(metric.unit === 'h' ? 8 : metric.unit === '%' ? 100 : 1,
    ...nights.map(n => Number.isFinite(n.metrics[metric.key]) ? Math.ceil(toChart(n.metrics[metric.key])) : 0));
  const step = 270/nights.length, x = index => 46+step*(index+.5);
  const y = value => 180-toChart(value)/maximum*142;
  const choose = event => {
    const rect=event.currentTarget.getBoundingClientRect();
    if (rect.width) setSelected(Math.max(0,Math.min(nights.length-1,
      Math.floor(((event.clientX-rect.left)/rect.width*340-46)/step))));
  };
  const time = value => value ? new Intl.DateTimeFormat('en-US',{timeZone:report.timezone,
    month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}).format(new Date(value)) : 'Unavailable';
  return <section className="sleep-report workout-session" aria-label="Sleep report">
    <div className="workout-card-header"><div><span className="workout-eyebrow">YOUR SLEEP · {source.label}</span>
      <h3>Your nights, at a glance</h3><p>{label(report.start)}–{label(report.end)} · {report.timezone.replace(/_/g,' ')}</p></div>
      <span className="workout-card-icon" aria-hidden="true">☾</span></div>
    {report.sources.length > 1 ? <div className="sleep-source-options" role="group" aria-label="Sleep data source">
      {report.sources.map(item => <button key={item.id} type="button" aria-pressed={source.id === item.id}
        onClick={() => {setSource(item.id);setSelected(latest(item));}}>{item.label}</button>)}
    </div> : null}
    <div className="sleep-metric-options" role="group" aria-label="Chart metric">
      {sleepMetrics.map(item => <button key={item.key} type="button" aria-pressed={metric.key === item.key}
        onClick={() => setMetric(item.key)}>{item.label}</button>)}
    </div>
    <div className="workout-chart-heading"><strong>{metric.label} by sleep date</strong><span>{metric.unit}</span></div>
    <svg className="sleep-chart" viewBox="0 0 340 216" role="img" aria-label={`${source.label} ${metric.label}. Missing dates remain blank.`}
      tabIndex={0} onPointerDown={choose} onPointerMove={event => {if(event.buttons) choose(event);}}
      onKeyDown={event => {
        if(!['ArrowLeft','ArrowRight'].includes(event.key)) return;
        event.preventDefault();setSelected(Math.max(0,Math.min(nights.length-1,selected+(event.key === 'ArrowRight' ? 1 : -1))));
      }}>
      <title>{source.label} recorded {metric.label}; use arrow keys or tap a date</title>
      <defs><linearGradient id={`${id}-sleep`} x1="0" x2="0" y1="1" y2="0"><stop offset="0%" stopColor="#748fe8"/><stop offset="100%" stopColor="#bcabff"/></linearGradient></defs>
      {[0,maximum/2,maximum].map(value => <g key={value}><line className="workout-grid-line" x1="46" x2="316" y1={180-value/maximum*142} y2={180-value/maximum*142} stroke="currentColor"/>
        <text x="39" y={184-value/maximum*142} textAnchor="end">{Math.round(value*10)/10}</text></g>)}
      {nights.map((night,index) => {
        const value=night.metrics[metric.key], present=Number.isFinite(value);
        const width=Math.min(24,step*.65);
        return <g key={night.date} className={present ? 'sleep-observed' : 'sleep-missing'}>
          {present ? <rect x={x(index)-width/2} y={y(value)} width={width} height={Math.max(1,180-y(value))} rx="3"
            fill={`url(#${id}-sleep)`} opacity={selected===index ? 1 : .65}/>
            : <rect x={x(index)-width/2} y="165" width={width} height="15" rx="3" fill="none" stroke="currentColor" strokeDasharray="2 3" opacity=".45"/>}
          <title>{label(night.date)}: {sleepValue(value,metric)}</title>
          {selected===index ? <circle cx={x(index)} cy="190" r="3" fill="#c0b1ff"/> : null}
          {nights.length<=7 || index===0 || index===nights.length-1 || index===Math.floor(nights.length/2)
            ? <text x={x(index)} y="208" textAnchor="middle">{new Date(`${night.date}T00:00:00Z`).getUTCDate()}</text> : null}
        </g>;
      })}
    </svg>
    <p className="workout-chart-note">Tap a date or use the arrow keys. Dashed outlines mean unavailable, not zero. {source.id === 'oura' ? 'Dates use Oura’s sleep-day labels.' : 'Dates use the local wake-up day.'}</p>
    <div className="sleep-summary"><div><span>Average {metric.label.toLowerCase()}</span><strong>{sleepValue(average.value,metric)}</strong></div>
      <div><span>Dates with this reading</span><strong>{average.count} / {nights.length}</strong></div></div>
    <div className="sleep-night" aria-live="polite"><div className="sleep-night-heading"><strong>{label(active.date)}</strong>
      <select aria-label="Select sleep date" value={active.date} onChange={event => setSelected(nights.findIndex(n => n.date===event.target.value))}>
        {nights.map(n => <option key={n.date} value={n.date}>{label(n.date)}</option>)}
      </select></div>
      <p>{active.selection === 'main' ? 'Oura main sleep' : active.selection === 'longest' ? 'Longest recorded session' : 'No main sleep session available'}
        {active.otherSessions ? ` · ${active.otherSessions} other session${active.otherSessions===1 ? '' : 's'} not added` : ''}</p>
      <dl className="sleep-details">{sleepMetrics.map(item => <div key={item.key}><dt>{item.label}</dt><dd>{sleepValue(active.metrics[item.key],item)}</dd></div>)}
        <div><dt>Session start</dt><dd>{time(active.start)}</dd></div><div><dt>Session end</dt><dd>{time(active.end)}</dd></div></dl></div>
    <div className="workout-metric-explanation" role="status"><strong>{metric.label}</strong><p>{metric.explanation}</p></div>
    <p className="workout-chart-note">Averages use only displayed readings. Sources stay separate; this view does not sum naps or overlapping device records.
      {source.status==='permission_missing' ? ' Oura sleep access is missing.' : !['ok','connected'].includes(source.status) ? ' This source could not be read.' : ''}
      {source.incomplete || source.capped ? ' This source reports incomplete or capped coverage.' : ''}</p>
  </section>;
}

export default function SleepReport({report}) {
  const normalized=normalizeSleepReport(report);
  return normalized ? <SleepCard key={`${normalized.start}-${normalized.end}`} report={normalized}/> : null;
}
