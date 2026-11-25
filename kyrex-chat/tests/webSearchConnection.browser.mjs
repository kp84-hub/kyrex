// Production Connections UI with synthetic accounts; no provider/key leaves localhost.
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';

const root = path.resolve('dist'), key = 'tvly-offline-browser-search-fixture';
let configured = false, requiresReconnect = false;
const mutations = [], errors = [];
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/auth/options' || url.pathname.startsWith('/api/')) {
    let data = { bots: [], providers: [], workspaces: [], conversations: [], delegations: [], relayed: [], connectors: [] };
    if (url.pathname === '/auth/options') data = { enabled: false };
    if (url.pathname === '/api/chat/onboarding') data = { required: false };
    if (url.pathname === '/api/connections/web-search') {
      if (req.method !== 'GET') {
        let body = ''; for await (const chunk of req) body += chunk;
        mutations.push({ method: req.method, body });
        if (req.method === 'PUT') { assert.deepEqual(JSON.parse(body), { key }); configured = true; requiresReconnect = false; }
        else if (req.method === 'DELETE') { configured = false; requiresReconnect = false; }
      }
      data = { provider: 'tavily', configured, requires_reconnect: requiresReconnect };
    }
    res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(data)); return;
  }
  try {
    const target = path.resolve(root, '.' + (url.pathname === '/' ? '/index.html' : url.pathname));
    if (!target.startsWith(root + path.sep)) throw new Error('Invalid path');
    const content = await fs.readFile(target);
    res.writeHead(200, { 'Content-Type': target.endsWith('.js') ? 'text/javascript' : target.endsWith('.css') ? 'text/css' : 'text/html' });
    res.end(content);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
let browser;
try {
  browser = await chromium.launch({ headless: true, executablePath: process.env.KYREX_TEST_CHROMIUM || undefined,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  for (const [width, height] of [[390, 844], [844, 390], [1280, 900]]) {
    configured = false; requiresReconnect = width === 390;
    const page = await browser.newPage({ viewport: { width, height } });
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://127.0.0.1:' + server.address().port + '/?settings=voice');
    const section = page.getByLabel('Web search connection', { exact: true });
    await section.locator('summary').click();
    if (requiresReconnect) {
      await section.getByText('Web search now uses Tavily. Replace your saved Brave key with a Tavily key to reconnect.', { exact: true }).waitFor();
      assert.equal(await section.getByRole('button', { name: 'Disconnect web search', exact: true }).count(), 1);
    }
    const input = section.getByLabel('Tavily Search API key', { exact: true });
    await input.fill(key);
    await section.getByRole('button', { name: 'Save key', exact: true }).click();
    await section.getByText('Key saved. API access will be checked when you search.', { exact: true }).waitFor();
    assert.equal(await input.inputValue(), '');
    assert.equal((await section.textContent()).includes('Replace your saved Brave key'), false);
    assert.equal((await section.textContent()).includes(key), false);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.equal(await page.evaluate(() => Object.values(localStorage).some(value => value.includes('tvly-offline-browser-search-fixture'))), false);
    await section.getByRole('button', { name: 'Disconnect web search', exact: true }).click();
    await section.getByRole('button', { name: 'Confirm disconnect', exact: true }).click();
    await section.getByText('Web search disconnected.', { exact: true }).waitFor();
    await page.close();
  }
  assert.deepEqual(errors, []);
  assert.equal(mutations.length, 6);
  console.log('Production web search settings passed in phone portrait, landscape and desktop layouts.');
} finally {
  await browser?.close();
  await new Promise(resolve => server.close(resolve));
}
