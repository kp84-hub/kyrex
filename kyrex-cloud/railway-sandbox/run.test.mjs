import assert from 'node:assert/strict';
import test from 'node:test';
import { verifyRift } from './run.mjs';

function fixture({ replies = [], destroyFails = false, onExec } = {}) {
  const controller = new AbortController();
  const calls = [];
  const progress = [];
  const args = {
    token: 'pilot-token', environmentId: 'pilot', base: 'a'.repeat(40), tree: 'b'.repeat(40),
    patch: Buffer.from('diff'), signal: controller.signal, progress: stage => progress.push(stage),
    create: async options => {
      calls.push(['create', options]);
      return {
        exec: async (command, options) => {
          calls.push(['exec', command, options]);
          onExec?.(controller, command);
          options.signal.throwIfAborted();
          return replies.shift() ?? { exitCode: 0 };
        },
        files: { write: async (...args) => calls.push(['write', ...args]) },
        destroy: async () => {
          calls.push(['destroy']);
          if (destroyFails) throw new Error('provider error');
        },
      };
    },
  };
  return { args, calls, progress, controller };
}

test('the isolated VM verifies the exact tree before running the fixed suite and destroys it', async () => {
  const { args, calls, progress } = fixture({ replies: [{ exitCode: 0 }, { exitCode: 0 }, { exitCode: 1 }] });
  assert.deepEqual(await verifyRift(args), { outcome: 'failure', exitCode: 1 });
  assert.deepEqual(calls.map(call => call[0]), ['create', 'exec', 'write', 'exec', 'exec', 'destroy']);
  assert.equal(calls[0][1].networkIsolation, 'ISOLATED');
  assert.equal(calls[0][1].environmentId, 'pilot');
  assert.equal(calls[0][1].idleTimeoutMinutes, 2);
  assert.equal(calls[0][1].env, undefined);
  assert.match(calls[1][1], new RegExp(args.base));
  assert.match(calls[3][1], new RegExp(args.tree));
  assert.match(calls[4][1], /^node --test /);
  assert.deepEqual(progress, ['copying', 'testing', 'cleaning']);
});

test('a snapshot mismatch prevents the test command and destroys the VM', async () => {
  const { args, calls } = fixture({ replies: [{ exitCode: 0 }, { exitCode: 1 }] });
  assert.deepEqual(await verifyRift(args), { outcome: 'unavailable', stage: 'copying' });
  assert.equal(calls.filter(call => call[0] === 'exec').length, 2);
  assert.deepEqual(calls.at(-1), ['destroy']);
});

test('a cancelled test still destroys the VM', async () => {
  const { args, calls } = fixture({ onExec: (controller, command) => {
    if (command.startsWith('node --test')) controller.abort();
  } });
  assert.deepEqual(await verifyRift(args), { outcome: 'cancelled', stage: 'testing' });
  assert.deepEqual(calls.at(-1), ['destroy']);
});

test('an unconfirmed destruction is reported and a pre-cancelled run never provisions', async () => {
  const running = fixture({ destroyFails: true });
  assert.deepEqual(await verifyRift(running.args), { outcome: 'unavailable', stage: 'cleaning' });
  const stopped = fixture();
  stopped.controller.abort();
  assert.deepEqual(await verifyRift(stopped.args), { outcome: 'cancelled', stage: 'starting' });
  assert.deepEqual(stopped.calls, []);
});
