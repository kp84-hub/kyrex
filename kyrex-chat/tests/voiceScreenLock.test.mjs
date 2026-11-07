import assert from 'node:assert/strict';
import test from 'node:test';
import { VoiceScreenLock } from '../src/lib/voiceScreenLock.js';
import { VoiceClient } from '../src/lib/voiceClient.js';

function sentinel() {
  const Target = globalThis.window?.EventTarget || globalThis.EventTarget;
  const Released = globalThis.window?.Event || globalThis.Event;
  const lock = new Target();
  lock.released = false; lock.releases = 0;
  lock.release = async () => {
    lock.releases++; lock.released = true; lock.dispatchEvent(new Released('release'));
  };
  return lock;
}
const settle = () => new Promise(resolve => setImmediate(resolve));
function fixture(request = async () => sentinel()) {
  const calls = [];
  const screen = new VoiceScreenLock({ visible: true, wakeLock: { request: type => {
    calls.push(type); return request();
  } } });
  return { screen, calls };
}

test('one screen lock covers the whole active voice conversation and releases on Stop', async () => {
  const lock = sentinel(), { screen, calls } = fixture(async () => lock);
  const client = new VoiceClient({ screenLock: screen, clientId: 'screen-test' });
  assert.equal(calls.length, 0);
  for (const phase of ['starting', 'loading', 'listening', 'recording', 'transcribing',
    'working', 'synthesizing', 'speaking', 'followup_wait', 'followup', 'interrupting']) client.state(phase);
  await settle();
  assert.deepEqual(calls, ['screen']);
  assert.equal(lock.released, false);
  await client.stop();
  assert.equal(lock.releases, 1);
});

test('hidden wake sessions release and reacquire when returning without resubmitting work', async () => {
  const locks = [], { screen, calls } = fixture(async () => { const lock = sentinel(); locks.push(lock); return lock; });
  const client = new VoiceClient({ screenLock: screen, clientId: 'screen-test',
    fetcher: () => { throw new Error('Screen visibility must not submit a voice command'); } });
  client.wake = true; client.state('listening'); await settle();
  await client.visibilityChanged(true);
  assert.equal(locks[0].released, true); assert.equal(client.phase, 'listening');
  await client.visibilityChanged(false); await settle();
  assert.deepEqual(calls, ['screen', 'screen']);
  assert.equal(locks[1].released, false);
  await client.stop();
});

test('stop during a pending request releases its late result without replacing a new session lock', async () => {
  let resolveOld;
  const old = sentinel(), current = sentinel();
  const { screen } = fixture(() => !resolveOld ? new Promise(resolve => { resolveOld = resolve; }) : Promise.resolve(current));
  screen.setActive(true); await settle();
  screen.setActive(false); screen.setActive(true); await settle();
  resolveOld(old); await settle();
  assert.equal(old.releases, 1); assert.equal(current.released, false);
  screen.setActive(false); assert.equal(current.releases, 1);
});

test('denied or unsupported wake locks never fail voice or spin on phase changes', async () => {
  const { screen, calls } = fixture(() => Promise.reject(new Error('Power saving policy')));
  const client = new VoiceClient({ screenLock: screen, clientId: 'screen-test' });
  client.state('recording'); await settle();
  client.state('working'); client.state('speaking'); await settle();
  assert.equal(client.phase, 'speaking'); assert.equal(calls.length, 1);
  await client.stop();
  const unsupported = new VoiceScreenLock({ wakeLock: {}, visible: true });
  unsupported.setActive(true); unsupported.setActive(false); await settle();
});

test('system release waits for a visibility event instead of retrying against battery policy', async () => {
  const { screen, calls } = fixture();
  screen.setActive(true); await settle();
  await screen.lock.release(); await settle();
  assert.equal(calls.length, 1);
  screen.setVisible(false); screen.setVisible(true); await settle();
  assert.equal(calls.length, 2); screen.setActive(false);
});

test('startup failure and another-device state release the local screen lock', async () => {
  for (const terminal of ['elsewhere', 'error']) {
    const lock = sentinel(), { screen } = fixture(async () => lock);
    const client = new VoiceClient({ screenLock: screen, clientId: 'screen-test' });
    client.state('starting'); await settle();
    if (terminal === 'elsewhere') client.state('elsewhere');
    else await client.fail(new Error('Microphone ended'));
    assert.equal(lock.releases, 1);
    await client.stop(); assert.equal(lock.releases, 1);
  }
});
