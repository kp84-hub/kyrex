// Browser fixture: real attachment/composer rendering, no account or Groq call.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
const { chromium } = await import(process.env.KYREX_TEST_PLAYWRIGHT_MODULE || 'playwright');
const cid = 'a'.repeat(32), iid = 'b'.repeat(32), root = resolve('dist');
const pixels = await readFile(resolve('../kyrex-desktop/public/companion.png'));
const posts = [], errors = [];
let messages = [];
const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname.startsWith('/api/')) {
    let data = { bots: [], providers: [], workspaces: [], delegations: [], relayed: [] };
    if (url.pathname === '/api/chat/onboarding') data = { required: false };
    if (url.pathname === '/api/bots') data = { bots: [{ id: 'chief', name: 'The Overwatcher', status: 'running', available: true }] };
    if (url.pathname === '/api/conversations') data = { conversations: [{ conversation_id: cid, title: 'Kbot image', bot_id: 'chief' }] };
    if (url.pathname === `/api/conversations/${cid}`) data = { conversation_id: cid, bot_id: 'chief', messages };
    if (url.pathname === `/api/conversations/${cid}/images`) {
      let size = 0; for await (const chunk of req) size += chunk.length;
      assert.ok(size > 1000);
      data = { id: iid, url: `/api/conversations/${cid}/images/${iid}` };
    }
    if (url.pathname === `/api/conversations/${cid}/images/${iid}`) {
      res.writeHead(200, { 'Content-Type': 'image/png' }); res.end(pixels); return;
    }
    if (url.pathname === '/api/chat') {
      let body = ''; for await (const chunk of req) body += chunk;
      const post = JSON.parse(body); posts.push(post);
      assert.deepEqual(post.image_ids, [iid]);
      messages = [{ id: 'u', role: 'user', content: post.message, images: [{ id: iid, url: `/api/conversations/${cid}/images/${iid}` }] },
        { id: 'r', role: 'assistant', content: 'Kbot has blue eyes and rocket boots.' }];
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      res.end('data: ' + JSON.stringify({ type: 'done', conversation_id: cid, content: messages[1].content }) + '\n\n'); return;
    }
    res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(data)); return;
  }
  try {
    const file = resolve(root, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
    if (!file.startsWith(root + '/')) { res.writeHead(404); res.end(); return; }
    res.writeHead(200, { 'Content-Type': ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' })[extname(file)] || 'application/octet-stream' });
    res.end(await readFile(file));
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const base = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ headless: true, executablePath: process.env.KYREX_TEST_CHROMIUM || undefined,
  args: ['--no-sandbox', '--disable-dev-shm-usage'] });
try {
  for (const [width, height, compact] of [[320, 300, true], [390, 844, false], [1280, 800, false]]) {
    messages = [];
    const page = await browser.newPage({ viewport: { width, height } });
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(id => localStorage.setItem('kyrex-chat.activeConversationId', id), cid);
    await page.goto(base + (compact ? '/?surface=kbot' : '/'));
    await page.getByRole('button', { name: 'Attach images', exact: true }).waitFor();
    await page.locator('input[type=file]').setInputFiles({ name: 'Kbot.png', mimeType: 'image/png', buffer: pixels });
    await page.getByAltText('Attached image 1', { exact: true }).waitFor();
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    if (process.env.KYREX_VISION_SCREENSHOT && compact) await page.screenshot({ path: process.env.KYREX_VISION_SCREENSHOT });
    await page.getByRole('button', { name: 'Send message', exact: true }).click();
    await page.getByText('Kbot has blue eyes and rocket boots.', { exact: true }).waitFor();
    await page.getByAltText('Uploaded image 1', { exact: true }).waitFor();
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.close();
  }
  assert.equal(posts.length, 3); assert.deepEqual(errors, []);
  console.log('Kbot, mobile and desktop image previews/upload/answer rendering passed');
} finally { await browser.close(); await new Promise(done => server.close(done)); }
