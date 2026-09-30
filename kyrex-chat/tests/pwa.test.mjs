import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const pub = new URL('../public/', import.meta.url);
const manifest = JSON.parse(await readFile(new URL('manifest.webmanifest', pub), 'utf8'));
assert.equal(manifest.display, 'standalone');
assert.equal(manifest.start_url, '/');
assert.equal(manifest.scope, '/');
for (const icon of manifest.icons) {
  const bytes = await readFile(new URL(icon.src.slice(1), pub));
  assert.equal(`${bytes.readUInt32BE(16)}x${bytes.readUInt32BE(20)}`, icon.sizes);
}
const listeners = {};
let precached;
let networkFails = false;
const cached = { offline: true };
const fetch = async () => { if (networkFails) throw new Error('offline'); return { fresh: true }; };
vm.runInNewContext(await readFile(new URL('sw.js', pub), 'utf8'), {
  URL, Promise, fetch,
  self: { location: { origin: 'https://chat.kyrex.dev' },
    addEventListener: (type, cb) => { listeners[type] = cb; },
    skipWaiting: async () => {}, clients: { claim: async () => {} } },
  caches: {
    open: async () => ({ addAll: async (urls) => { precached = [...urls]; } }),
    match: async () => cached,
    keys: async () => [], delete: async () => {},
  },
});
let work;
listeners.install({ waitUntil: (promise) => { work = promise; } });
await work;
assert.deepEqual(precached, ['/offline.html', '/icons/icon-192.png']);
function request(path, mode = 'cors', method = 'GET') {
  let response;
  listeners.fetch({ request: { url: `https://chat.kyrex.dev${path}`, mode, method },
    respondWith: (promise) => { response = promise; } });
  return response;
}
for (const path of ['/api/chat', '/api/task', '/auth/login', '/auth/callback', '/assets/app.js']) {
  assert.equal(request(path, 'navigate'), undefined, path);
}
assert.equal(request('/', 'navigate', 'POST'), undefined);
assert.deepEqual(await request('/', 'navigate'), { fresh: true });
networkFails = true;
assert.equal(await request('/', 'navigate'), cached);
assert.equal(await request('/icons/icon-192.png'), cached);
console.log('PWA manifest/icons and worker: private routes bypassed, fresh network, offline fallback passed.');
