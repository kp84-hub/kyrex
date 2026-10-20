import assert from 'node:assert/strict';
import { createServer } from 'vite';

const { chromium } = await import(process.env.KYREX_TEST_PLAYWRIGHT_MODULE || 'playwright');
const server = await createServer({ server: { host: '127.0.0.1', port: 0 }, logLevel: 'error' });
await server.listen();
const browser = await chromium.launch({ headless: true,
  executablePath: process.env.KYREX_TEST_BROWSER_EXECUTABLE || undefined,
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
try {
  const context = await browser.newContext({ permissions: ['clipboard-read', 'clipboard-write'] });
  const page = await context.newPage();
  await page.goto(`http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/messageSelection.html`);
  await page.locator('[data-message-selection]').nth(1).waitFor();
  await page.waitForTimeout(100);
  const point = (selector, offset) => page.locator(selector).evaluate((element, offset) => {
    const range = document.createRange();
    range.setStart(element.firstChild, offset); range.setEnd(element.firstChild, offset + 1);
    const box = range.getBoundingClientRect();
    return { x: box.x, y: box.y + box.height / 2 };
  }, offset);
  const drag = async (from, to) => {
    await page.mouse.move(from.x, from.y); await page.mouse.down();
    await page.mouse.move(to.x, to.y, { steps: 20 }); await page.mouse.up();
    await page.waitForTimeout(100);
  };
  const selected = () => page.evaluate(() => getSelection().toString());
  const copy = async expected => {
    await page.keyboard.press('Control+c');
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), expected);
  };
  const partialStart = await point('.message-assistant p:first-child', 5);
  const partialEnd = await point('.message-assistant li:last-child', 12);
  await drag(partialStart, partialEnd);
  const partial = await selected();
  assert.ok(partial.includes('first paragraph') && partial.includes('Morning class'));
  await copy(partial);
  await page.mouse.click(partialStart.x + 10, partialStart.y, { button: 'right' });
  await page.keyboard.press('Escape');
  assert.equal(await selected(), partial, 'Opening the native context menu must preserve the selection.');
  await copy(partial);
  await page.evaluate(() => window.renderTranscript([{ id: 'background', role: 'assistant', content: 'Background update.' }]));
  await page.waitForTimeout(150);
  assert.equal(await selected(), partial, 'Idle transcript refresh/new results must preserve the selected DOM text.');
  const promptStart = await point('.message-user [data-message-selection]', 7);
  await drag(promptStart, partialEnd);
  const across = await selected();
  assert.ok(across.includes('first paragraph'), 'A normal drag into the answer must not be clipped to the prompt.');
  await copy(across);
  await drag(partialEnd, promptStart);
  const backward = await selected();
  assert.ok(backward.includes('show my calendar') && backward.includes('first paragraph'));
  await copy(backward);

  // Ctrl+A scopes to the clicked message; native/browser-menu Select all
  // also scopes once, without an endless selectionchange/range-rewrite loop.
  await page.mouse.click(promptStart.x, promptStart.y);
  await page.keyboard.press('Control+a');
  assert.equal(await selected(), 'Please show my calendar for today.');
  await copy(await selected());
  await page.mouse.click(partialStart.x, partialStart.y);
  await page.evaluate(() => {
    window.selectionChanges = 0;
    document.addEventListener('selectionchange', () => window.selectionChanges++);
    document.execCommand('selectAll');
  });
  await page.waitForFunction(() => getSelection().toString().startsWith('This is the first paragraph'));
  const nativeAll = await selected();
  assert.ok(!nativeAll.includes('Please show') && !nativeAll.includes('Background update'));
  const changes = await page.evaluate(() => window.selectionChanges);
  await page.waitForTimeout(200);
  assert.equal(await selected(), nativeAll);
  assert.equal(await page.evaluate(() => window.selectionChanges), changes, 'The highlight must settle after Select all.');
  await copy(nativeAll);

  const composer = page.getByRole('textbox', { name: 'Composer' });
  await composer.focus(); await page.keyboard.press('Control+a');
  await copy('An unsent draft');
  assert.equal(await composer.inputValue(), 'An unsent draft');
  console.log('Chromium: partial/backward/cross-message drags, clipboard, context menu, refresh, scoped Select all and composer editing passed.');
} finally {
  await browser.close(); await server.close();
}
