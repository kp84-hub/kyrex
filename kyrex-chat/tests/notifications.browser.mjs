import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
const { chromium } = await import(process.env.KYREX_TEST_PLAYWRIGHT_MODULE || 'playwright');
const root = resolve('dist'), id = 'b'.repeat(32), cid = 'a'.repeat(32), old = 'd'.repeat(32);
let read = false;
const requests = [];
const item = () => ({ id, conversation: cid, kind: 'done', title: 'Automation · Task finished', created: 1000, read: read ? 2000 : null });
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost'); requests.push([req.method, url.pathname]);
  if (url.pathname.startsWith('/api/')) {
    let data = {};
    if (url.pathname === '/api/chat/onboarding') data = { required: false, step: 'ready' };
    else if (url.pathname === '/api/notifications') data = { notifications: [item()], unread: read ? 0 : 1 };
    else if (url.pathname === '/api/notifications/' + id) data = item();
    else if (url.pathname === '/api/notifications/read') { read = true; data = { updated: true }; }
    else if (url.pathname === '/api/notifications/settings') data = { available: true, public_key: 'B' + 'A'.repeat(86) };
    else if (url.pathname.startsWith('/api/notifications/devices/')) data = { enabled: false };
    else if (url.pathname === '/api/conversations') data = { conversations: [old, cid].map(conversation_id => ({ conversation_id, title: conversation_id === cid ? 'Automation result' : 'Original chat' })) };
    else if (url.pathname === '/api/conversations/' + cid || url.pathname === '/api/conversations/' + old) {
      const conversation_id = url.pathname.split('/').at(-1);
      data = { conversation_id, messages: [{ id: 'message', role: 'assistant', content: conversation_id === cid ? 'Your saved automation result is here.' : 'Original conversation.' }] };
    } else data = { bots: [], providers: [], workspaces: [], delegations: [], relayed: [], authenticated: true, available: true };
    res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(data)); return;
  }
  if (url.pathname === '/blank.html') { res.writeHead(200, { 'Content-Type': 'text/html' }); res.end('<!doctype html><title>Background test</title>'); return; }
  const file = resolve(root, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
  if (!file.startsWith(root + '/')) { res.writeHead(404); res.end(); return; }
  try {
    const body = await readFile(file);
    res.writeHead(200, { 'Content-Type': ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png' })[extname(file)] || 'application/octet-stream' }); res.end(body);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ headless: true, executablePath: process.env.KYREX_TEST_BROWSER_EXECUTABLE || undefined, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
try {
  const context = await browser.newContext({ permissions: ['notifications'], viewport: { width: 390, height: 844 } });
  await context.addInitScript(old => localStorage.setItem('kyrex-chat.activeConversationId', old), old);
  const page = await context.newPage(), errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const cdp = await context.newCDPSession(page), registrations = [];
  cdp.on('ServiceWorker.workerRegistrationUpdated', event => registrations.push(...event.registrations));
  await cdp.send('ServiceWorker.enable');
  await page.goto(origin);
  const bell = page.getByRole('button', { name: 'Notifications, 1 unread', exact: true });
  await bell.waitFor(); await bell.click();
  await page.getByRole('dialog', { name: 'Notifications', exact: true }).waitFor();
  await page.getByRole('button', { name: 'Enable device notifications', exact: true }).waitFor();
  assert.ok(!requests.some(([method, path]) => method === 'POST' && path.includes('/devices/')), 'Opening Chat never enrolls a device');
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Mobile header must fit beside the Bot and voice controls');
  await page.screenshot({ path: '/tmp/kyrex-notifications-mobile.png' });
  await page.locator('.notification-item').click();
  await page.getByText('Your saved automation result is here.', { exact: true }).waitFor();
  assert.ok(read); assert.ok(!requests.some(([method, path]) => method === 'POST' && /\/approve|\/respond|\/api\/task$|\/api\/chat$/.test(path)));
  await page.evaluate(() => navigator.serviceWorker.ready);
  assert.ok(registrations.length > 0);
  const registrationId = registrations.filter(r => !r.isDeleted).at(-1).registrationId;
  // Chat is no longer loaded. DevTools delivers a controlled Push event to
  // the actual installed worker; Chromium displays a real notification.
  await page.goto(origin + '/blank.html');
  await cdp.send('ServiceWorker.deliverPushMessage', { origin, registrationId, data: JSON.stringify({ id, body: 'Private email text', url: 'https://foreign.example' }) });
  const captured = await page.waitForFunction(async () => {
    const shown = await (await navigator.serviceWorker.ready).getNotifications();
    return shown.length ? shown.map(n => ({ title: n.title, body: n.body, tag: n.tag, data: n.data })) : false;
  });
  const shown = await captured.jsonValue();
  assert.equal(shown[0].title, 'Kyrex Chat'); assert.equal(shown[0].tag, 'kyrex-' + id);
  assert.ok(!JSON.stringify(shown).includes('Private email'));
  // This is the URL constructed by the worker's click handler. Authentication
  // and conversation selection still use the regular application APIs.
  await page.goto(origin + '/?notification=' + id);
  await page.getByText('Your saved automation result is here.', { exact: true }).waitFor();
  await page.waitForTimeout(150);
  assert.equal(await page.evaluate(() => localStorage.getItem('kyrex-chat.activeConversationId')), cid);
  assert.deepEqual(errors, []);
  console.log('Chromium: mobile bell, owner-resolved conversation, background Push event, generic OS notification and deep link passed. No live push service was called.');
} finally { await browser.close(); await new Promise(done => server.close(done)); }
