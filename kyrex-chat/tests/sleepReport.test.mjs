import test from 'node:test';
import assert from 'node:assert/strict';
import React, {act} from 'react';
import {createRoot} from 'react-dom/client';
import Message from '../src/components/Message.jsx';
import {normalizeSleepReport,sleepMetrics,sleepAverage} from '../src/lib/sleepReport.js';
import {consumeStream} from '../src/lib/streaming.js';
import {sanitizeConversation} from '../src/lib/sanitize.js';

const night=(date,duration) => ({date,start:`${date}T00:00:00-04:00`,end:`${date}T08:00:00-04:00`,selection:'main',other_sessions:1,
  metrics:{total_sleep_seconds:duration,time_in_bed_seconds:28800,efficiency_pct:90,heart_rate_bpm:52,hrv_ms:45}});
const report={version:1,start_date:'2026-10-03',end_date:'2026-10-05',timezone:'America/New_York',sources:[
  {id:'oura',status:'ok',nights:[night('2026-10-03',25200),night('2026-10-05',21600)]},
  {id:'samsung_health',status:'connected',incomplete:true,nights:[{...night('2026-10-03',27000),selection:'longest',metrics:{total_sleep_seconds:27000,time_in_bed_seconds:28800}}]},
]};
function render(message) {
  const container=document.createElement('div');document.body.append(container);const root=createRoot(container);
  act(()=>root.render(React.createElement(Message,{message,isLastAssistant:true})));
  return {container,dispose:()=>{act(()=>root.unmount());container.remove();}};
}

test('sleep bars and averages use observed nights only, with selectable metrics and dates',()=>{
  const {container,dispose}=render({id:'s',role:'assistant',content:'',sleep_report:report});
  assert.equal(container.querySelectorAll('.sleep-observed').length,2);
  assert.equal(container.querySelectorAll('.sleep-missing').length,1);
  assert.match(container.querySelector('.sleep-summary').textContent,/6h 30m/);
  assert.match(container.querySelector('.sleep-summary').textContent,/2 \/ 3/);
  assert.match(container.querySelector('.sleep-night').textContent,/Oct 5/);
  const svg=container.querySelector('svg');
  act(()=>svg.dispatchEvent(new window.KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true})));
  assert.match(container.querySelector('.sleep-night').textContent,/No main sleep session available/);
  assert.match(container.querySelector('.sleep-night').textContent,/Unavailable/);
  const select=container.querySelector('select');
  act(()=>{select.value='2026-10-03';select.dispatchEvent(new window.Event('change',{bubbles:true}));});
  assert.match(container.querySelector('.sleep-night').textContent,/1 other session not added/);
  assert.match(container.querySelector('.sleep-night').textContent,/12:00 AM/);
  const efficiency=[...container.querySelectorAll('.sleep-metric-options button')].find(b=>b.textContent==='Efficiency');
  act(()=>efficiency.click());
  assert.equal(efficiency.getAttribute('aria-pressed'),'true');
  assert.match(container.querySelector('.workout-metric-explanation').textContent,/percentage/);
  assert.match(container.querySelector('.sleep-summary').textContent,/90 %/);
  dispose();
});

test('switching devices keeps overlapping sleep totals separate and preserves missing metrics',()=>{
  const {container,dispose}=render({id:'s',role:'assistant',content:'',sleep_report:report});
  act(()=>[...container.querySelectorAll('.sleep-source-options button')].find(b=>b.textContent==='Samsung Health').click());
  assert.match(container.querySelector('.sleep-summary').textContent,/7h 30m/);
  assert.equal(container.querySelectorAll('.sleep-observed').length,1);
  assert.match(container.textContent,/incomplete or capped coverage/);
  act(()=>[...container.querySelectorAll('.sleep-metric-options button')].find(b=>b.textContent==='HRV').click());
  assert.equal(container.querySelectorAll('.sleep-observed').length,0);
  assert.match(container.querySelector('.sleep-summary').textContent,/Unavailable/);
  dispose();
});

test('invalid dates, durations and failed sources cannot invent bars; measured zero remains a value',()=>{
  const invalid=structuredClone(report);
  invalid.sources[0].nights[0].metrics.total_sleep_seconds=Infinity;
  invalid.sources[0].nights[1].metrics.total_sleep_seconds=0;
  invalid.sources[0].label='<img src=x onerror=alert(1)>';
  const normalized=normalizeSleepReport(invalid);
  assert.deepEqual(sleepAverage(normalized.sources[0].nights,sleepMetrics[0]),{count:1,value:0});
  invalid.sources[1].status='failed';
  assert.ok(normalizeSleepReport(invalid).sources[1].nights.every(n=>!Object.keys(n.metrics).length));
  assert.equal(normalizeSleepReport({...report,start_date:'2026-02-30'}),null);
  assert.equal(normalizeSleepReport({...report,end_date:'2026-12-01'}),null);
  const {container,dispose}=render({id:'s',role:'assistant',content:'',sleep_report:invalid});
  assert.equal(container.querySelector('img'),null);
  assert.equal(container.querySelectorAll('.sleep-observed').length,1);
  assert.match(container.querySelector('.sleep-summary').textContent,/0h 00m/);
  assert.ok(!container.querySelector('svg').innerHTML.match(/NaN|Infinity/));dispose();
});

test('streaming and terminal-only sleep cards survive saved transcript reload',async()=>{
  for(const intermediate of [true,false]) {
    async function* stream(){if(intermediate)yield {type:'sleep_report',report};yield {type:'done',content:'',sleep_report:report};}
    const seen=[];const {terminal}=await consumeStream(stream(),{onSleepReport:r=>seen.push(r)});
    assert.equal(terminal.kind,'done');assert.deepEqual(terminal.sleep_report,report);assert.equal(seen.length,intermediate?1:0);
    const stored=sanitizeConversation({messages:[{id:'s',role:'assistant',content:'',sleep_report:terminal.sleep_report}]});
    const {container,dispose}=render(stored.messages[0]);assert.ok(container.querySelector('.sleep-chart'));dispose();
  }
});

test('user text cannot attach a native sleep report',()=>{
  const {container,dispose}=render({id:'s',role:'user',content:'Graph my sleep',sleep_report:report});
  assert.equal(container.querySelector('svg'),null);dispose();
});
