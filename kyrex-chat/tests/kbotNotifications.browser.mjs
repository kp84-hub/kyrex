// Run after building Chat. Like notifications.browser.mjs, this uses a local
// API fixture; no account, real notifications, microphone or task is used.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
const { chromium } = await import(process.env.KYREX_TEST_PLAYWRIGHT_MODULE || 'playwright');
const root = resolve('dist'), shell = resolve('../kyrex-desktop/src');
const cid = 'a'.repeat(32), old = 'd'.repeat(32), id = 'b'.repeat(32);
const requests = [], commands = [], errors = [];
let read = false, closes = 0;
const items = () => Array.from({ length: 8 }, (_, i) => ({
  id: i ? String(i).repeat(32) : id, conversation: cid, kind: i ? 'done' : 'awaiting_approval',
  title: i ? `Automation ${i} · Task finished` : 'Automation · Task review needed',
  created: 1792778000 - i * 600, read: !i && read ? 1792779000 : null,
}));
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  requests.push([req.method, url.pathname]);
  if (url.pathname === '/auth/options') {
    res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ enabled: false })); return;
  }
  if (url.pathname.startsWith('/api/')) {
    let data = {};
    if (url.pathname === '/api/chat/onboarding') data = { required: false };
    else if (url.pathname === '/api/notifications') data = { notifications: items(), unread: read ? 7 : 8 };
    else if (url.pathname === '/api/notifications/' + id) data = items()[0];
    else if (url.pathname === '/api/notifications/read') {
      let body = ''; for await (const chunk of req) body += chunk;
      assert.deepEqual(JSON.parse(body).ids, [id]); read = true; data = { updated: true };
    } else if (url.pathname === '/api/conversations') data = { conversations: [old, cid].map(conversation_id => ({ conversation_id, title: conversation_id === cid ? 'Automation result' : 'Original chat' })) };
    else if ([old, cid].some(value => url.pathname === '/api/conversations/' + value)) {
      const conversation_id = url.pathname.split('/').at(-1);
      data = { conversation_id, messages: [{ id: 'message', role: 'assistant', content: conversation_id === cid ? 'Review the saved task here.' : 'Original conversation.' }] };
    } else data = { bots: [], providers: [], workspaces: [], delegations: [], relayed: [] };
    res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(data)); return;
  }
  if (url.pathname === '/__companion__/hide') { closes++; res.end('Hide requested'); return; }
  if (url.pathname === '/launcher.html') {
    res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'");
    res.setHeader('Content-Type', 'text/html');
    res.end('<link rel="stylesheet" href="/launcher.css"><div id="app"></div><script type="module" src="/launcher-fixture.js"></script>'); return;
  }
  if (url.pathname === '/launcher-fixture.js') {
    res.setHeader('Content-Type', 'text/javascript');
    res.end("import {render} from '/launcher-ui.js';window.companionEvents={};render(document.querySelector('#app'),{mode:'launcher',invoke:async(name,data)=>{await window.nativeCommand(name,data);return{unread:8};},listen:async(name,fn)=>{window.companionEvents[name]=fn;return()=>delete window.companionEvents[name];}});"); return;
  }
  try {
    const file = url.pathname === '/launcher-ui.js' ? resolve(shell, 'ui.js') : url.pathname === '/launcher.css' ? resolve(shell, 'style.css') : resolve(root, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
    if (!file.startsWith(root + '/') && ![resolve(shell, 'ui.js'), resolve(shell, 'style.css')].includes(file)) { res.writeHead(404); res.end(); return; }
    const content = url.pathname === '/companion.png' ? await readFile(resolve(shell, '../public/companion.png')) : await readFile(file);
    res.writeHead(200, { 'Content-Type': ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png' })[extname(file)] || 'application/octet-stream' }); res.end(content);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ headless: true, executablePath: process.env.KYREX_TEST_BROWSER_EXECUTABLE || undefined, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
try {
  const chat = await browser.newPage({ viewport: { width: 320, height: 300 }, serviceWorkers: 'block' });
  chat.on('pageerror', error => errors.push(error.message));
  await chat.addInitScript(() => { window.__kyrexKbotAnchor = { side: 'left', y: 240, size: 'compact' }; });
  // Emulate only the native presentation acknowledgement here. The Windows
  // smoke test exercises real WebView2 navigation interception and geometry.
  const sizes = { compact: { width: 320, height: 300 }, medium: { width: 640, height: 620 }, full: { width: 1280, height: 800 } };
  await chat.route(origin + '/__companion__/size/*', async route => {
    const size = new URL(route.request().url()).pathname.split('/').at(-1);
    assert.ok(sizes[size]);
    await route.abort('aborted');
    await chat.setViewportSize(sizes[size]);
    await chat.evaluate(size => {
      window.__kyrexKbotAnchor = { side: 'left', y: 240, size };
      window.dispatchEvent(new Event('kyrex-kbot-anchor'));
    }, size);
  });
  await chat.goto(origin + '/?surface=kbot&conversation=' + old);
  await chat.getByText('Original conversation.', { exact: true }).waitFor();
  assert.equal(await chat.locator('.notification-trigger').count(), 0);
  assert.equal(await chat.getByRole('dialog', { name: 'Notifications', exact: true }).count(), 0);
  const launcher = await browser.newPage({ viewport: { width: 84, height: 84 } });
  launcher.on('pageerror', error => errors.push(error.message));
  await launcher.exposeFunction('nativeCommand', async (name, data) => {
    commands.push([name, data]);
    if (name === 'open_chat' && data?.view === 'notifications') await chat.evaluate(() => {
      window.__kyrexKbotNotificationsPending = Date.now(); window.dispatchEvent(new Event('kyrex-kbot-notifications'));
    });
  });
  await launcher.goto(origin + '/launcher.html');
  await launcher.getByRole('button', { name: /Talk to Kbot/ }).waitFor();
  await launcher.mouse.move(40, 40); await launcher.mouse.down();
  await launcher.waitForTimeout(750); await launcher.mouse.up();
  const inbox = chat.getByRole('dialog', { name: 'Notifications', exact: true });
  await inbox.waitFor();
  if (process.env.KYREX_KBOT_NOTIFICATION_SCREENSHOT) await chat.screenshot({ path: process.env.KYREX_KBOT_NOTIFICATION_SCREENSHOT, omitBackground: true });
  assert.equal(commands.filter(([name]) => name === 'open_chat').length, 1);
  assert.ok(!commands.some(([name]) => name === 'toggle_chat'));
  assert.equal(read, false, 'Holding Kbot only reads the inbox list');
  assert.ok(!requests.some(([method]) => method === 'POST'));
  assert.ok(!requests.some(([, path]) => /\/devices\/|\/settings$/.test(path)));
  assert.ok(await chat.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await inbox.evaluate(el => { el.scrollTop = el.scrollHeight; });
  const close = chat.getByRole('button', { name: 'Close notifications', exact: true });
  const bounds = await close.boundingBox(); assert.ok(bounds.y >= 0 && bounds.y + bounds.height <= 300);
  await chat.keyboard.press('Escape');
  assert.equal(await inbox.count(), 0); assert.equal(closes, 0, 'Escape closes the inbox before the bubble');
  // Keyboard access opens the same inbox without another header button.
  await launcher.getByRole('button', { name: /Talk to Kbot/ }).focus(); await launcher.keyboard.press('n');
  await inbox.waitFor();
  await chat.locator('.notification-item').first().click();
  await chat.getByText('Review the saved task here.', { exact: true }).waitFor();
  assert.ok(read); assert.equal(await inbox.count(), 0);
  assert.ok(!requests.some(([method, path]) => method === 'POST' && /approve|respond|\/api\/task|\/api\/chat$/.test(path)));
  await chat.locator('.composer-input').fill('Keep this unsent draft');
  await chat.evaluate(() => { window.__kbotResizeDocument = document; });
  for (const size of ['medium', 'full', 'compact', 'medium']) {
    await chat.getByRole('combobox', { name: 'Kbot window size', exact: true }).selectOption(size);
    await chat.locator(`.kbot-app[data-size="${size}"]`).waitFor();
    assert.equal(await chat.locator('.composer-input').inputValue(), 'Keep this unsent draft');
    assert.equal(await chat.evaluate(() => window.__kbotResizeDocument === document), true);
    assert.equal(await chat.getByText('Review the saved task here.', { exact: true }).count(), 1);
    assert.ok(await chat.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    const sizeControl = await chat.getByRole('combobox', { name: 'Kbot window size', exact: true }).boundingBox();
    assert.ok(sizeControl.x >= 0 && sizeControl.x + sizeControl.width <= sizes[size].width);
    if (process.env.KYREX_KBOT_SIZE_SCREENSHOT_PREFIX) await chat.screenshot({ path: `${process.env.KYREX_KBOT_SIZE_SCREENSHOT_PREFIX}-${size}.png`, omitBackground: true });
  }
  const [full] = await Promise.all([
    chat.waitForEvent('popup'),
    chat.getByRole('link', { name: 'Open full Kyrex Chat in browser', exact: true }).click(),
  ]);
  full.on('pageerror', error => errors.push(error.message));
  await full.getByText('Review the saved task here.', { exact: true }).waitFor();
  assert.equal(new URL(full.url()).search, '?conversation=' + cid);
  assert.equal(await full.locator('.kbot-app').count(), 0);
  assert.equal(await chat.locator('.composer-input').inputValue(), 'Keep this unsent draft');
  assert.ok(new URL(chat.url()).searchParams.get('surface') === 'kbot');
  assert.ok(!requests.some(([method, path]) => method === 'POST' && /approve|respond|\/api\/task|\/api\/chat$/.test(path)), 'Expanding never sends the draft or replays a task');
  await full.close();
  const opened = commands.filter(([name]) => name === 'open_chat').length;
  await launcher.mouse.move(20, 20); await launcher.mouse.down(); await launcher.mouse.move(40, 20);
  await launcher.waitForTimeout(750); await launcher.mouse.up();
  assert.equal(commands.filter(([name]) => name === 'open_chat').length, opened, 'Dragging cancels the hold');
  assert.ok(commands.some(([name]) => name === 'drag_launcher'));
  // Fixed native mood events drive the actual bundled CSS, without another
  // button. Hover/focus and OS reduced motion report only local booleans.
  const mood = async value => launcher.evaluate(value => window.companionEvents['companion-motion']({payload:value}), value);
  await mood('sleeping');
  assert.equal(await launcher.locator('.sleep-eye').first().evaluate(el => getComputedStyle(el).display), 'block');
  assert.equal(await launcher.locator('.mascot-jet').first().evaluate(el => getComputedStyle(el).opacity), '0');
  await launcher.mouse.move(0, 0); await launcher.mouse.move(40, 40);
  assert.ok(commands.some(([name, data]) => name === 'launcher_activity' && data.hovered));
  await mood('waking');
  assert.equal(await launcher.locator('.mascot').evaluate(el => getComputedStyle(el).animationName), 'wake-stretch');
  await mood('bonk');
  assert.equal(await launcher.locator('.bonk-stars').evaluate(el => getComputedStyle(el).display), 'block');
  if (process.env.KYREX_KBOT_POSE_SCREENSHOT) await launcher.screenshot({path:process.env.KYREX_KBOT_POSE_SCREENSHOT,omitBackground:true});
  await mood('flying-left');
  assert.equal(await launcher.locator('.mascot').evaluate(el => getComputedStyle(el).animationName), 'flight-left');
  await launcher.emulateMedia({reducedMotion:'reduce'});
  await launcher.waitForTimeout(100);
  assert.ok(commands.some(([name, data]) => name === 'launcher_activity' && data.reducedMotion));
  assert.equal(await launcher.locator('.mascot').evaluate(el => getComputedStyle(el).animationName), 'none');
  assert.equal(await launcher.getByRole('button').count(), 1);
  assert.deepEqual(errors, []);
  console.log('Chromium: Kbot gestures, inbox, three window layouts with draft retained, full-browser conversation, flight poses and reduced motion passed.');
} finally { await browser.close(); await new Promise(done => server.close(done)); }
