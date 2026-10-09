import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import FitnessProfileSettings, { formToProfile, profileToForm } from '../src/components/FitnessProfileSettings.jsx';

let saved = { age:42, height_cm:177.8, weight_kg:90, goal:'endurance', usual_activity:'hiit' };
let rejectSave = false, rejectLoad = false;
const calls=[];
globalThis.fetch = async (url, options={}) => {
  calls.push({ url, ...options });
  const failed = options.method === 'PUT' ? rejectSave : (!options.method && rejectLoad);
  if (failed) return { ok:false,status:503,statusText:'Unavailable',async json(){return {detail:'Profile unavailable'};} };
  if (options.method === 'PUT') saved=JSON.parse(options.body);
  if (options.method === 'DELETE') saved={};
  return {ok:true,status:200,async json(){return saved;}};
};
const container=document.createElement('div'); document.body.appendChild(container);
let root=createRoot(container);
const field=(label)=>container.querySelector(`[aria-label="${label}"]`);
async function enter(label,value) {
  const node=field(label);
  await act(async ()=>{
    const prototype=node.tagName === 'SELECT' ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype,'value').set.call(node,value);
    node.dispatchEvent(new Event(node.tagName === 'SELECT' ? 'change' : 'input',{bubbles:true}));
  });
}
async function submit() { await act(async ()=>{container.querySelector('form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));}); }
await act(async ()=>{root.render(React.createElement(FitnessProfileSettings));});
assert.equal(field('Age (years)').value,'42');
assert.equal(field('Height (feet)').value,'5'); assert.equal(field('Height (inches)').value,'10');
assert.equal(field('Weight').value,'198.42');
assert.match(container.textContent,/shared with your selected model/);
assert.equal(calls[0].cache,'no-store');
await enter('Age (years)','43'); await enter('Fitness goal','strength');
await submit();
assert.equal(saved.age,43); assert.equal(saved.height_cm,177.8); assert.equal(saved.weight_kg,90);
assert.equal(saved.goal,'strength'); assert.match(container.querySelector('[role="status"]').textContent,/Profile saved/);
await enter('Fitness units','metric');
assert.equal(field('Height (cm)').value,'177.8'); assert.equal(field('Weight').value,'90');
await enter('Weight','88.5'); rejectSave=true; await submit();
assert.equal(saved.weight_kg,90); assert.equal(field('Weight').value,'88.5');
assert.match(container.querySelector('[role="alert"]').textContent,/Profile unavailable/);
rejectSave=false; await submit(); assert.equal(saved.weight_kg,88.5);
await act(async ()=>root.unmount()); root=createRoot(container);
await act(async ()=>root.render(React.createElement(FitnessProfileSettings)));
assert.equal(field('Age (years)').value,'43'); assert.equal(field('Fitness goal').value,'strength');
await act(async ()=>[...container.querySelectorAll('button')].find(b=>b.textContent==='Clear profile').click());
assert.deepEqual(saved,{}); assert.equal(field('Age (years)').value,'');
assert.match(container.querySelector('[role="status"]').textContent,/Earlier chat messages remain/);
assert.equal(calls.at(-1).method,'DELETE');
assert.ok(calls.every(call=>call.url==='/api/connections/fitness/profile'));
await act(async ()=>root.unmount()); rejectLoad=true; root=createRoot(container);
await act(async ()=>root.render(React.createElement(FitnessProfileSettings)));
assert.equal(container.querySelector('fieldset').disabled,true);
rejectLoad=false;
await act(async ()=>[...container.querySelectorAll('button')].find(b=>b.textContent==='Retry profile load').click());
assert.equal(container.querySelector('fieldset').disabled,false);
assert.deepEqual(formToProfile(profileToForm({})),{});
assert.deepEqual(formToProfile(profileToForm({height_cm:182.88,weight_kg:100},'us')), {height_cm:182.88,weight_kg:100});
await act(async ()=>root.unmount());
console.log('Fitness profile units, owner API, save/reload/clear and failure recovery passed.');
