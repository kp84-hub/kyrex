import assert from 'node:assert/strict';
import { reconcileTranscript } from '../src/lib/transcript.js';

const user = { id: 'turn-r1-user', role: 'user', content: 'Run task' };
const reply = { id: 'turn-r1-assistant', role: 'assistant', content: 'Finished', turn_user_id: user.id, persisted_id: 'task-t1-result' };
const current = [user, reply];
assert.deepEqual(reconcileTranscript(current, [user]), current, 'a snapshot with only the saved user turn cannot erase its streamed answer');
assert.deepEqual(reconcileTranscript(current, [user, { ...reply, content: '' }]), current, 'an empty placeholder cannot replace a finished reply');
const stored = [user, { id: 'task-t1-result', role: 'assistant', content: 'Finished with details' }];
const updated = reconcileTranscript(current, stored);
assert.equal(updated.length, 2);
assert.equal(updated[1].content, 'Finished with details', 'durable task output remains authoritative');
assert.equal(updated[1].id, reply.id, 'task reconciliation preserves the rendered message key');
assert.deepEqual(reconcileTranscript(updated, stored), updated, 'task reconciliation remains stable on repeated refreshes');

const connectorReply = { ...reply, persisted_id: undefined };
const connectorStored = [user, { id: 'turn-r1-user-usage', role: 'assistant', content: 'Finished' }];
assert.equal(reconcileTranscript([user, connectorReply], connectorStored).length, 2, 'specialized connector replies are reconciled within their own turn');
const otherTurn = [user, { id: 'turn-r2-user', role: 'user', content: 'Run task again' }, { id: 'turn-r2-assistant', role: 'assistant', content: 'Finished' }];
assert.deepEqual(reconcileTranscript([user, connectorReply], otherTurn), [user, connectorReply], 'an identical reply in another turn cannot acknowledge this result');
assert.equal(reconcileTranscript([user, { ...reply, error: 'Interrupted', content: '' }], [user]).length, 1, 'unsaved failed placeholders do not block durable updates');
console.log('stale transcript, task identity, connector identity and repeated refresh checks: passed');
