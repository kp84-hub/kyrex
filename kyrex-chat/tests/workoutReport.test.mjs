import test from 'node:test';
import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import Message from '../src/components/Message.jsx';
import { normalizeWorkoutReport, curveSegments } from '../src/lib/workoutReport.js';
import { consumeStream } from '../src/lib/streaming.js';
import { sanitizeConversation } from '../src/lib/sanitize.js';

const report = { version:1, timezone:'America/New_York', sessions:[{
  title:'Synthetic test workout', source:'Samsung Health', start:'2026-10-08T12:30:48Z', end:'2026-10-08T13:15:22Z',
  metrics:{duration_seconds:2674,heart_rate_avg_bpm:138,heart_rate_max_bpm:171,distance_meters:0},
  metric_status:{heart_rate:'ok',active_calories:'not_synced',total_calories:'permission_missing',distance:'ok',steps:'no_data'},
  heart_rate_series:[{offset_seconds:0,average_bpm:100,min_bpm:95,max_bpm:105},
    {offset_seconds:40,average_bpm:110,min_bpm:105,max_bpm:115},
    {offset_seconds:500,average_bpm:160,min_bpm:155,max_bpm:165,gap_before:true},
    {offset_seconds:540,average_bpm:165,min_bpm:160,max_bpm:171}],
  heart_rate_coverage:{continuity:'not_verified',query_capped:false}, needs_sync:true,
}]};

function render(message) {
  const container=document.createElement('div'); document.body.appendChild(container);
  const root=createRoot(container);
  act(() => root.render(React.createElement(Message,{message,isLastAssistant:true})));
  return {container,dispose:() => { act(() => root.unmount()); container.remove(); }};
}

test('native chart renders actual ranges, gaps, local times and missing values',() => {
  const {container,dispose}=render({id:'m',role:'assistant',content:'The observed samples rose.',workout_report:report});
  assert.match(container.textContent,/8:30 AM–9:15 AM/);
  assert.match(container.textContent,/138/); assert.match(container.textContent,/171/);
  assert.match(container.textContent,/Unavailable/); assert.match(container.textContent,/No re-pair is needed/);
  assert.match(container.textContent,/Recording continuity is not verified/);
  assert.equal(container.querySelectorAll('.workout-metric').length,7);
  assert.equal(container.querySelectorAll('svg path[stroke]').length,2,'curve must break across the gap');
  const distance=container.querySelectorAll('.workout-metric')[5];
  assert.match(distance.textContent,/0 km/,'measured zero must remain distinct from unavailable');
  const active=container.querySelectorAll('.workout-metric')[3];
  act(() => active.click());
  assert.equal(active.getAttribute('aria-pressed'),'true');
  assert.match(container.querySelector('.workout-metric-explanation').textContent,/excluding resting energy/);
  assert.match(container.querySelector('.workout-metric-explanation').textContent,/Sync the last seven days/);
  const svg=container.querySelector('svg');
  act(() => svg.dispatchEvent(new window.KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true})));
  assert.match(container.querySelector('.workout-chart-reading').textContent,/110 bpm/);
  dispose();
});

test('summary-only data cannot fabricate a curve and provider text stays escaped',() => {
  const empty=structuredClone(report); empty.sessions[0].heart_rate_series=[];
  empty.sessions[0].title='<img src=x onerror=alert(1)>';
  const {container,dispose}=render({id:'m',role:'assistant',content:'',workout_report:empty});
  assert.equal(container.querySelector('svg'),null);
  assert.match(container.textContent,/Summary values alone cannot create a trend/);
  assert.equal(container.querySelector('img'),null);
  dispose();
});

test('invalid coordinates and unexpected shapes never reach SVG geometry',() => {
  const invalid=structuredClone(report); invalid.sessions[0].heart_rate_series.push({offset_seconds:NaN,average_bpm:Infinity,min_bpm:90,max_bpm:150});
  assert.equal(normalizeWorkoutReport(invalid).sessions[0].series.length,4);
  assert.equal(normalizeWorkoutReport({version:1,sessions:[{start:'bad',end:'bad'}]}),null);
  assert.equal(normalizeWorkoutReport({version:999,sessions:[]}),null);
  assert.equal(curveSegments(normalizeWorkoutReport(report).sessions[0].series).length,2);
  assert.equal(curveSegments([{offset_seconds:0},{offset_seconds:300}]).length,1,
    'wide downsampling buckets alone must not invent a recording gap');
});

test('streamed and final-only cards survive terminal processing and transcript reload',async () => {
  for (const intermediate of [true,false]) {
    async function* stream() {
      if (intermediate) yield {type:'workout_report',report};
      yield {type:'done',content:'',workout_report:report};
    }
    const seen=[];
    const {terminal}=await consumeStream(stream(),{onWorkoutReport:r => seen.push(r)});
    assert.equal(terminal.kind,'done'); assert.deepEqual(terminal.workout_report,report);
    assert.equal(seen.length,intermediate ? 1 : 0);
    const stored=sanitizeConversation({messages:[{id:'m',role:'assistant',content:'',workout_report:terminal.workout_report}]});
    const {container,dispose}=render(stored.messages[0]);
    assert.ok(container.querySelector('svg')); dispose();
  }
});

test('user messages cannot attach a workout card',() => {
  const {container,dispose}=render({id:'m',role:'user',content:'Graph my workout',workout_report:report});
  assert.equal(container.querySelector('svg'),null); dispose();
});
