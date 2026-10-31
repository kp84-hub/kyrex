import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import CryptoPaperSettings from '../src/components/CryptoPaperSettings.jsx';
const base='/api/automations/crypto-paper', posts=[], opened=[];
let fail=false, modelStarted=false, enabled=false, configured=false, rows=[], selected=['BTC','ETH'], savedCandle=60, savedStrategy='Hold when uncertain.',savedRisk='standard';
const activePreset={strategy:'Use early paper momentum entries; hold when no setup qualifies.',candle_interval_minutes:5,interval_seconds:300,bot_id:'unrelated'};
const riskProfiles={standard:{max_order_usd:'25',max_position_usd:'100',daily_buy_usd:'200',pause_loss_usd:'50'},broad:{max_order_usd:'100',max_position_usd:'300',daily_buy_usd:'2000',pause_loss_usd:'100'}};
const broadPreset={strategy:'Review realized paper outcomes and choose supported opportunities.',risk_profile:'broad',candle_interval_minutes:5};
const available=['BTC','ETH','SOL','XRP','ADA','DOGE','LINK','AVAX'];
const metrics={cash_usd:'1000',equity_usd:'1000',pnl_usd:'0',benchmark_equity_usd:'1000',fees_usd:'0',max_drawdown_usd:'0',
 last_tick:null,auto_halted:false,positions:{BTC:{quantity:'0',basis:'0'},ETH:{quantity:'0',basis:'0'}}};
const bots=()=>modelStarted?[{id:'paper',name:'Crypto Bot',model:'my-choice',status:'running'}]:[];
function view(offset=0){return {risk_profiles:riskProfiles,broad_preset:broadPreset,daily_activity:{day:'2026-10-30',time_zone:'UTC',buys_usd:'200',sells_usd:'25',turnover_usd:'225',buy_remaining_usd:savedRisk==='broad'?'1800':'0'},active_preset:activePreset,available_symbols:available,mode:'paper',configured,settings:{enabled,bot_id:configured?'paper':'',interval_seconds:900,candle_interval_minutes:savedCandle,risk_profile:savedRisk,
 symbols:selected,strategy:savedStrategy,time_zone:'UTC',conversation_id:configured?'ongoing-paper':null,detail:'Saved paper status.'},
 bots:bots(),bot_template:{name:'Crypto Bot',systemPrompt:'Paper only',policy:{'crypto:read':0,'crypto:paper':0,'*':'deny'}},
 limits:riskProfiles[savedRisk],metrics,ticks:rows.slice(offset,offset+1),tick_count:rows.length,history_truncated:rows.length>1,
 next_offset:offset+1<rows.length?offset+1:null,benchmark:'Fixed benchmark.',limitation:'Simulated fills only.'};}
const response=value=>({ok:true,status:200,json:async()=>value});
globalThis.fetch=async(url,options={})=>{
 const method=options.method || 'GET',body=options.body?JSON.parse(options.body):null;
 if(method!=='GET')posts.push({url,method,body});
 if(url==='/api/bots'&&method==='POST')return response({id:'paper'});
 if(url==='/api/bots/paper'&&method==='PATCH'){modelStarted=true;return response(bots()[0]);}
 if(url.startsWith(base)){
  if(fail)throw new Error('Paper service unavailable');
  if(method==='PUT'){configured=true;enabled=true;assert.equal(body.bot_id,'paper');assert.equal(body.interval_seconds,900);assert.deepEqual(body.symbols,available);assert.equal(body.candle_interval_minutes,5);selected=body.symbols;savedCandle=body.candle_interval_minutes;savedStrategy=body.strategy;savedRisk=body.risk_profile;}
  if(url===base+'/pause')enabled=false;
  return response(view(Number(new URL(url,'https://test.example').searchParams.get('offset')||0)));
 }
 throw new Error('Unexpected request '+url);
};
const node=document.body.appendChild(document.createElement('div')),root=createRoot(node);
const button=text=>[...node.querySelectorAll('button')].find(b=>b.textContent===text);
const change=async(label,value)=>act(async()=>{
 const element=[...node.querySelectorAll('label')].find(l=>l.textContent.startsWith(label)).querySelector('select');
 element.value=value;element.dispatchEvent(new Event('change',{bubbles:true}));
});
try{
 await act(async()=>root.render(React.createElement(CryptoPaperSettings,{profiles:[{id:'p',name:'Saved provider',models:['my-choice']}],onOpenConversation:id=>opened.push(id)})));
 assert.equal(posts.length,0,'Opening Settings never creates a Bot or enables inference.');
 assert.match(node.textContent,/Daily buying.*200.*200.*0.*remaining/);assert.match(node.textContent,/blocks new buys, not sales/);
 assert.match(node.textContent,/Simulated money/);assert.match(node.textContent,/96 model calls/);
 await change('Provider','p');await change('Model','my-choice');
 await act(async()=>button('Create Crypto Bot').closest('form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
 assert.equal(posts.find(p=>p.url==='/api/bots').body.model,'my-choice');
 assert.equal(posts.find(p=>p.url==='/api/bots').body.provider_profile_id,'p');
 assert.ok(!posts.some(p=>p.url===base&&p.method==='PUT'),'Creating the Bot alone does not enable checks.');
 assert.equal(node.querySelectorAll('input[type=checkbox]:checked').length,2);
 for(const checkbox of node.querySelectorAll('input[type=checkbox]:checked'))await act(async()=>checkbox.click());
 assert.equal(button('Start paper experiment').disabled,true,'An empty watchlist cannot be saved.');
 await act(async()=>button('Watch all eight coins').click());
 assert.equal(node.querySelectorAll('input[type=checkbox]:checked').length,8);
 const writesBefore=posts.length;
 await act(async()=>button('Use aggressive paper preset').click());
 assert.equal(posts.length,writesBefore,'Loading a preset does not save or start checks.');
 assert.match(node.querySelector('[role="status"]').textContent,/Save paper settings to apply/);
 assert.equal(node.querySelector('textarea').value,activePreset.strategy);
 await act(async()=>button('Start paper experiment').closest('form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
 assert.equal(enabled,true);assert.match(node.textContent,/Running · Paper/);assert.match(node.textContent,/Saved analysis: 5-minute candles/);
 assert.equal(savedRisk,'standard');assert.equal(savedStrategy,activePreset.strategy);
 const beforeBroad=posts.length;
 await act(async()=>button('Use broader paper test').click());
 assert.equal(posts.length,beforeBroad,'Loading broader limits is a preview, not account activation.');
 assert.equal(savedRisk,'standard');assert.match(node.textContent,/Saved risk profile: Standard paper limits/);
 assert.equal(node.querySelector('textarea').value,broadPreset.strategy);
 await act(async()=>button('Save paper settings').closest('form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
 assert.equal(savedRisk,'broad');assert.match(node.textContent,/Saved risk profile: Broader paper test/);
 assert.match(node.textContent,/Saved paper limits: \$100\.00 per order.*\$300\.00 per position.*\$2,000\.00/);
 assert.equal(posts.filter(p=>p.method==='PUT').at(-1).body.strategy,broadPreset.strategy);
 await act(async()=>button('Open Crypto Bot chat').click());assert.deepEqual(opened,['ongoing-paper']);
 const tick=id=>({task_id:id,created:1000,decision:{action:'hold',symbol:'BTC',reason:'<script>untrusted evidence</script>'},outcome:'held',fill:null,
  model:{model:'my-choice'},execution_quotes:{retrieved_at:1000}});
 rows=[tick('one'),tick('two')];
 await act(async()=>button('Refresh paper results').click());
 assert.equal(node.querySelectorAll('.budget-entry').length,1);assert.equal(node.querySelector('script'),null);
 await act(async()=>button('Earlier decisions').click());assert.equal(node.querySelectorAll('.budget-entry').length,2);
 await act(async()=>button('Pause paper experiment').click());assert.equal(enabled,false);
 assert.ok(posts.some(p=>p.url===base+'/pause'));
 fail=true;await act(async()=>button('Refresh paper results').click());
 assert.match(node.querySelector('[role="alert"]').textContent,/unavailable/);
 assert.ok(button('Pause paper experiment'),'Pause remains available even after a failed read.');
 assert.ok(!posts.some(p=>/orders|withdraw|exchange/.test(p.url)));
}finally{await act(async()=>root.unmount());node.remove();}
console.log('Paper settings preserve model choice, explicit start/pause, history pagination, safe reasons and saved chat binding: passed');
