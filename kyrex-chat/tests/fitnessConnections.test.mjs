import assert from 'node:assert/strict';
import { connectorById, connectorCard, buildHubModel } from '../src/lib/connectorRegistry.js';
import { consentUrl } from '../src/lib/consentWindow.js';
globalThis.window = { location: { origin: 'https://chat.example' } };
assert.equal(consentUrl('https://cloud.ouraring.com/oauth/authorize?state=test'), 'https://cloud.ouraring.com/oauth/authorize?state=test');
for (const url of ['https://cloud.ouraring.com.evil.test/oauth/authorize','https://cloud.ouraring.com/elsewhere','http://cloud.ouraring.com/oauth/authorize','https://user@cloud.ouraring.com/oauth/authorize']) assert.throws(() => consentUrl(url));
for (const id of ['oura', 'samsung_health']) {
  const connector = connectorById(id);
  assert.equal(connectorCard(connector, null).connectable, false);
  const view = { provider: id, connected: true, status: 'connected', configured: true, synced_at: 100,
    device_token: 'never show', access_token: 'never show',
    capabilities: { bots: { fitness_reader: { capabilities: ['fitness.read'] } } } };
  const card = connectorCard(connector, view);
  assert.equal(card.connectable, true); assert.equal(card.connected, true); assert.equal(card.syncedAt, 100);
  assert.ok(!JSON.stringify(card).includes('never show'));
  assert.equal(buildHubModel([view], id).connected[0].id, id);
}
console.log('Fitness cards require backend support, exclude secrets and restrict Oura consent URLs: passed');
