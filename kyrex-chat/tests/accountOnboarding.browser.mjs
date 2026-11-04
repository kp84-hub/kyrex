// Production-bundle layout and account-gate checks with synthetic API responses.
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';

const root = path.resolve('dist');
const server = http.createServer(async (req, res) => {
  try {
    const pathname = new URL(req.url, 'http://localhost').pathname;
    const target = path.resolve(root, '.' + (pathname === '/' ? '/index.html' : pathname));
    if (!target.startsWith(root + path.sep)) throw new Error('Invalid path');
    const content = await fs.readFile(target);
    const mime = target.endsWith('.js') ? 'text/javascript' : target.endsWith('.css') ? 'text/css' : 'text/html';
    res.writeHead(200, { 'Content-Type': mime }); res.end(content);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = 'http://127.0.0.1:' + server.address().port;
let browser;
try {
  browser = await chromium.launch({ headless: true, ...(process.env.KYREX_TEST_CHROMIUM ? { executablePath: process.env.KYREX_TEST_CHROMIUM } : {}) });
  const page = await browser.newPage();
  const errors = [], requests = [];
  let managed = false;
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/auth/options', route => route.fulfill({ json: { enabled: true, google: true, github: true, email: true, managed: true, signup_mode: 'invite' } }));
  await page.route('**/api/**', route => {
    const pathname = new URL(route.request().url()).pathname;
    requests.push(pathname);
    const json = pathname === '/api/me' ? { authenticated: managed }
      : pathname === '/api/account' ? { account: { id: 'synthetic', owner: 'account_test', kind: 'managed', complete: true, name: 'Avery' } }
      : { conversations: [] };
    return route.fulfill({ json });
  });
  for (const viewport of [{ width: 1100, height: 820 }, { width: 390, height: 844 }, { width: 320, height: 300 }]) {
    await page.setViewportSize(viewport);
    await page.goto(origin);
    await page.getByRole('heading', { name: 'Welcome to Kyrex' }).waitFor();
    assert.equal(await page.getByRole('button', { name: 'Continue with Google' }).isDisabled(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.equal(requests.some(p => /provider|bots|connections|onboarding/.test(p)), false);
    await page.getByText('Pair a parent-managed device', { exact: true }).click();
    await page.getByRole('textbox', { name: 'Pairing code' }).fill('synthetic-pairing-code');
    assert.equal(await page.getByRole('button', { name: 'Pair this device' }).isEnabled(), true);
  }
  for (const viewport of [{ width: 320, height: 300 }, { width: 640, height: 620 }, { width: 980, height: 860 }]) {
    await page.setViewportSize(viewport);
    await page.goto(origin + '/?surface=kbot');
    await page.getByRole('heading', { name: 'Sign in to Kbot' }).waitFor();
    assert.equal(await page.getByRole('link', { name: 'Sign in in browser' }).getAttribute('href'), '/auth/login');
    assert.equal(await page.getByRole('link', { name: 'Hide Kbot' }).count(), 1);
    assert.equal(await page.locator('form, input, details').count(), 0);
    const layout = await page.evaluate(() => {
      const root = document.getElementById('root');
      const panel = document.querySelector('.kbot-account-frame > .main');
      const account = document.querySelector('.account-welcome');
      return { background: getComputedStyle(panel).backgroundColor,
        outerScroll: root.scrollHeight > root.clientHeight || document.documentElement.scrollHeight > innerHeight,
        horizontalScroll: account.scrollWidth > account.clientWidth,
        accountScroll: getComputedStyle(account).overflowY };
    });
    assert.equal(layout.background, 'rgb(17, 25, 34)', 'Account content has an opaque chat panel');
    assert.equal(layout.outerScroll, false, 'Only the account panel may scroll');
    assert.equal(layout.horizontalScroll, false);
    assert.equal(layout.accountScroll, 'auto');
    await page.screenshot({ path: '/tmp/kbot-signin-' + viewport.width + '.png' });
  }
  managed = true; requests.length = 0;
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(origin);
  await page.getByRole('main', { name: 'Managed learning chat' }).waitFor();
  assert.equal(await page.locator('input[type=file]').count(), 0);
  assert.equal(await page.getByRole('textbox', { name: 'Message Kyrex' }).count(), 1);
  assert.equal(requests.some(p => /provider|bots|connections|onboarding/.test(p)), false);
  assert.deepEqual(errors, []);
  console.log('Production account gate and managed chat passed at desktop, phone and compact sizes.');
} finally {
  await browser?.close();
  await new Promise(resolve => server.close(resolve));
}
