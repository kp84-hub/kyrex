// Public offline page only. Chats, API responses, authentication, and app
// bundles always use the network so private data and old releases aren't cached.
const CACHE = 'kyrex-chat-public-v1';
self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) =>
    cache.addAll(['/offline.html', '/icons/icon-192.png'])
  ).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(
    keys.filter((key) => key.startsWith('kyrex-chat-public-') && key !== CACHE)
      .map((key) => caches.delete(key))
  )).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || event.request.method !== 'GET') return;
  if (event.request.mode === 'navigate' && ['/', '/index.html'].includes(url.pathname)) {
    event.respondWith(fetch(event.request).catch(() => caches.match('/offline.html')));
  } else if (url.pathname === '/icons/icon-192.png') {
    event.respondWith(fetch(event.request).catch(() => caches.match('/icons/icon-192.png')));
  }
});
