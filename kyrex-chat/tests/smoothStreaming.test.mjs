import assert from 'node:assert/strict';
import test from 'node:test';
import { createTextStreamSmoother } from '../src/lib/smoothStreaming.js';

function clock() {
  let next = 1;
  const callbacks = new Map();
  return {
    raf(fn) { const id = next++; callbacks.set(id, fn); return id; },
    caf(id) { callbacks.delete(id); },
    step() {
      const current = [...callbacks.values()];
      callbacks.clear();
      current.forEach((fn) => fn());
    },
    pending() { return callbacks.size; },
  };
}

test('irregular chunks become progressive frames and finish authoritatively', async () => {
  const c = clock();
  const seen = [];
  let finished = null;
  const smoother = createTextStreamSmoother((text) => seen.push(text), {
    raf: c.raf, caf: c.caf, baseChars: 2,
  });
  smoother.push('Hello');
  smoother.push(' world');
  smoother.finish('Hello world!', (text) => { finished = text; });
  while (c.pending()) c.step();
  await smoother.whenIdle();
  assert.ok(seen.length > 2);
  assert.equal(seen.at(-1), 'Hello world!');
  assert.equal(finished, 'Hello world!');
});

test('authoritative non-prefix replaces streamed text without duplication', () => {
  const c = clock();
  const seen = [];
  const smoother = createTextStreamSmoother((text) => seen.push(text), {
    raf: c.raf, caf: c.caf, baseChars: 20,
  });
  smoother.push('partial');
  c.step();
  smoother.finish('correct final');
  while (c.pending()) c.step();
  assert.equal(seen.at(-1), 'correct final');
  assert.ok(!seen.at(-1).includes('partialcorrect'));
});

test('cancel stops pending animation work', async () => {
  const c = clock();
  const seen = [];
  const smoother = createTextStreamSmoother((text) => seen.push(text), {
    raf: c.raf, caf: c.caf,
  });
  smoother.push('never rendered');
  smoother.cancel();
  c.step();
  await smoother.whenIdle();
  assert.deepEqual(seen, []);
});
