// Public offline page only. Chats, API responses, authentication, and app
// bundles always use the network so private data and old releases aren't cached.
const CACHE = 'kyrex-chat-public-v2';
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

// Push contains an opaque inbox identity only. The authenticated application
// resolves its conversation; neither payload content nor a URL can approve or
// execute a task. Notification content stays generic on a locked/shared device.
self.addEventListener('push', event => {
  event.waitUntil((async () => {
    let identity = '';
    try { identity = event.data?.json()?.id || ''; } catch {}
    if (!/^[a-f0-9]{32}$/.test(identity)) return;
    await self.registration.showNotification('Kyrex Chat', {
      body: 'You have an update. Open Kyrex to review it.',
      icon: '/icons/icon-192.png', badge: '/icons/icon-192.png',
      tag: 'kyrex-' + identity, data: { id: identity },
    });
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    windows.forEach(client => client.postMessage({ type: 'kyrex-notifications' }));
  })());
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const identity = event.notification.data?.id;
  if (!/^[a-f0-9]{32}$/.test(identity || '')) return;
  event.waitUntil((async () => {
    const url = new URL('/?notification=' + identity, self.location.origin).href;
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of windows) {
      if (new URL(client.url).origin === self.location.origin && 'navigate' in client) {
        const opened = await client.navigate(url); if (opened) return opened.focus();
      }
    }
    return self.clients.openWindow(url);
  })());
});
