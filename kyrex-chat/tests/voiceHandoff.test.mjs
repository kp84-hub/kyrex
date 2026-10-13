import assert from 'node:assert/strict';
import { VoiceClient } from '../src/lib/voiceClient.js';

const until = async condition => {
  for (let n = 0; n < 100 && !condition(); n++) await new Promise(r => setTimeout(r, 1));
  assert.ok(condition(), 'Voice did not reach the expected state');
};
const json = value => ({ ok: true, json: async () => value });
function fixture({ companion = true, denied = false, race = false } = {}) {
  const calls = []; let opened = 0, closed = 0, terminated = 0, worker, owner = 'phone';
  const client = new VoiceClient({ clientId: 'f'.repeat(32),
    fetcher: async (url, init) => {
      calls.push([url, init]);
      if (url.endsWith('/session')) return json({ conversation_id: 'voice-alice',
        settings: { configured: true }, active_device: companion ? { name: 'Android phone' } : null });
      if (url.endsWith('/listen')) {
        if (race && !JSON.parse(init.body).takeover) return { ok: false,
          headers: new Headers({ 'X-Voice-Reason': 'active' }), json: async () => ({ detail: 'Voice is listening on Android phone.' }) };
        owner = 'browser'; return json({ lease: 'test-only-browser-lease' });
      }
      if (url.endsWith('/stop')) owner = null;
      return json({});
    },
    microphone: async () => { opened++; if (denied) throw new DOMException('Permission denied', 'NotAllowedError');
      return { stop: () => closed++, tone() {} }; },
    workerFactory: () => (worker = { postMessage() {}, terminate: () => terminated++ }) });
  return { client, calls, get worker() { return worker; }, get opened() { return opened; },
    get closed() { return closed; }, get terminated() { return terminated; }, get owner() { return owner; } };
}

for (const wake of [false, true]) {
  const f = fixture(); await f.client.start(wake);
  assert.equal(f.client.phase, 'elsewhere'); assert.equal(f.opened, 0);
  assert.deepEqual(f.calls.map(([url]) => url), ['/api/voice/session']);
  await f.client.stop(); assert.equal(f.owner, 'phone', 'Leaving Chat preserves companion listening');
}
const denied = fixture({ denied: true }); await denied.client.start(true, true);
assert.equal(denied.client.phase, 'stopped'); assert.equal(denied.owner, 'phone');
assert.ok(!denied.calls.some(([url]) => url.endsWith('/listen') || url.endsWith('/stop')));

const broken = fixture(); const brokenStart = broken.client.start(true, true);
await until(() => broken.worker); broken.worker.onmessage({ data: { type: 'error', error: 'Wake runtime unavailable' } });
await brokenStart; assert.equal(broken.owner, 'phone'); assert.equal(broken.closed, 1);
assert.equal(broken.terminated, 1); assert.equal(broken.client.phase, 'stopped');

const cancelled = fixture(); const cancelledStart = cancelled.client.start(true, true);
await until(() => cancelled.worker); await cancelled.client.stop(); await cancelledStart;
assert.equal(cancelled.owner, 'phone'); assert.equal(cancelled.closed, 1);
cancelled.worker.onmessage({ data: { type: 'ready' } }); assert.equal(cancelled.client.phase, 'stopped');

const switched = fixture(); const switching = switched.client.start(true, true);
await until(() => switched.worker);
assert.equal(switched.owner, 'phone', 'Wake startup does not take the companion lease');
switched.worker.onmessage({ data: { type: 'wake' } });
assert.equal(switched.client.phase, 'loading', 'Early wake events cannot capture without a lease');
switched.worker.onmessage({ data: { type: 'ready' } }); await switching;
assert.equal(switched.owner, 'browser'); assert.equal(switched.client.phase, 'listening');
assert.equal(JSON.parse(switched.calls.find(([url]) => url.endsWith('/listen'))[1].body).takeover, true);
await switched.client.stop(); assert.equal(switched.owner, null);

const raced = fixture({ companion: false, race: true }); const racing = raced.client.start(true);
await until(() => raced.worker); raced.worker.onmessage({ data: { type: 'ready' } }); await racing;
assert.equal(raced.client.phase, 'elsewhere'); assert.equal(raced.owner, 'phone');
assert.equal(raced.closed, 1); assert.equal(raced.terminated, 1);
assert.equal(JSON.parse(raced.calls.find(([url]) => url.endsWith('/listen'))[1].body).takeover, false);
assert.ok(!raced.calls.some(([url]) => url.endsWith('/stop')));
console.log('Companion preservation, explicit handoff, startup failures, cancellation and competing claims: passed');
