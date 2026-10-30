// Actual mobile component and production CSS. No exchange or model calls.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { build } from 'esbuild';
const { chromium } = await import(process.env.KYREX_TEST_PLAYWRIGHT_MODULE || 'playwright');
const built=await build({stdin:{contents:`import React from 'react';import {createRoot} from 'react-dom/client';import Settings from './src/components/CryptoPaperSettings.jsx';createRoot(document.getElementById('root')).render(React.createElement(Settings,{profiles:[{id:'saved',name:'Saved provider',models:['chosen-model']}]}));`,resolveDir:process.cwd(),loader:'jsx'},bundle:true,write:false,format:'iife',define:{'process.env.NODE_ENV':'"production"'}});
const script=built.outputFiles[0].text,css=await readFile('src/App.css');
const available=['BTC','ETH','SOL','XRP','ADA','DOGE','LINK','AVAX'];
let ready=false,enabled=false,configured=false;const writes=[];
const data=()=>({available_symbols:available,mode:'paper',configured,settings:{enabled,bot_id:ready?'crypto':'',interval_seconds:900,symbols:available,strategy:'Prefer cash when uncertain.',time_zone:'UTC',conversation_id:configured?'paper-chat':null,detail:'Saved paper status.'},
 bots:ready?[{id:'crypto',name:'Crypto Bot',status:'running',model:'chosen-model'}]:[],bot_template:{name:'Crypto Bot',systemPrompt:'Paper only',policy:{'crypto:read':0,'crypto:paper':0,'*':'deny'}},
 metrics:{cash_usd:'1000',equity_usd:'1000',pnl_usd:'0',benchmark_equity_usd:'1000',fees_usd:'0',max_drawdown_usd:'0',positions:{BTC:{quantity:'0',basis:'0'},ETH:{quantity:'0',basis:'0'}},last_tick:null,auto_halted:false},
 ticks:[],tick_count:0,history_truncated:false,next_offset:null,benchmark:'Fixed benchmark.',limitation:'Simulated fills only.'});
const server=createServer(async(req,res)=>{
 if(req.url==='/app.js'){res.writeHead(200,{'Content-Type':'text/javascript'});return res.end(script);}
 if(req.url==='/app.css'){res.writeHead(200,{'Content-Type':'text/css'});return res.end(css);}
 if(req.url.startsWith('/api/')){
  if(req.method!=='GET'){let body='';for await(const chunk of req)body+=chunk;writes.push({url:req.url,method:req.method,body:body?JSON.parse(body):null});}
  let value=data();
  if(req.url==='/api/bots'&&req.method==='POST')value={id:'crypto'};
  if(req.url==='/api/bots/crypto'&&req.method==='PATCH'){ready=true;value={id:'crypto',status:'running'};}
  if(req.url==='/api/automations/crypto-paper'&&req.method==='PUT'){configured=true;enabled=true;value=data();}
  if(req.url==='/api/automations/crypto-paper/pause'){enabled=false;value=data();}
  res.writeHead(200,{'Content-Type':'application/json'});return res.end(JSON.stringify(value));
 }
 res.writeHead(200,{'Content-Type':'text/html'});res.end('<!doctype html><html><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/app.css"><body style="height:auto;overflow:auto"><main style="padding:14px;max-width:750px;margin:auto;box-sizing:border-box" id="root"></main><script src="/app.js"></script></body></html>');
});
await new Promise(done=>server.listen(0,'127.0.0.1',done));
const browser=await chromium.launch({headless:true,executablePath:process.env.KYREX_TEST_BROWSER_EXECUTABLE||undefined,args:['--no-sandbox']});
try{
 const page=await browser.newPage({viewport:{width:390,height:844}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://127.0.0.1:${server.address().port}`);
 await page.getByRole('button',{name:'Create Crypto Bot',exact:true}).waitFor();assert.equal(writes.length,0);
 await page.getByLabel('Crypto provider',{exact:true}).selectOption('saved');await page.getByLabel('Crypto model',{exact:true}).selectOption('chosen-model');
 await page.getByRole('button',{name:'Create Crypto Bot',exact:true}).click();
 await page.getByRole('button',{name:'Start paper experiment',exact:true}).waitFor();
 assert.equal(writes.filter(w=>w.method==='PUT').length,0);
 assert.equal(await page.getByRole('checkbox').count(),8);
 await page.getByRole('checkbox',{name:'SOL',exact:true}).uncheck();
 await page.getByRole('button',{name:'Watch all eight coins',exact:true}).click();
 assert.equal(await page.locator('input[type=checkbox]:checked').count(),8);
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Paper settings fit a phone viewport.');
 assert.equal(await page.getByLabel('Check frequency').evaluate(el=>getComputedStyle(el).colorScheme),'dark');
 await page.getByRole('button',{name:'Start paper experiment',exact:true}).click();
 await page.getByText('Running · Paper',{exact:true}).waitFor();
 assert.deepEqual(writes.find(w=>w.method==='PUT').body.symbols,available);
 await page.getByRole('button',{name:'Pause paper experiment',exact:true}).click();
 await page.getByText('Paused · Paper',{exact:true}).waitFor();
 assert.ok(!writes.some(w=>/orders|withdraw/.test(w.url)));assert.deepEqual(errors,[]);
 await page.screenshot({path:'/tmp/kyrex-crypto-paper-mobile.png',fullPage:true});
 console.log('Chromium: dark mobile paper settings, chosen model, explicit start and pause, no real-order path passed.');
}finally{await browser.close();await new Promise(done=>server.close(done));}
