const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const { EventEmitter } = require('node:events');
const { JSDOM } = require('jsdom');
const tick = () => new Promise(resolve => setImmediate(resolve));
function harness(initial = {}, env = {}, fetchImpl) {
  const settings = { provider: 'openai', model: '', apiKey: '', baseUrl: '', ...initial };
  const overrides = { ...(initial.workspaceOverrides || {}) };
  const commands = new Map(), children = [], messages = [], secrets = new Map();
  const prompts = [], information = [], timers = new Map();
  let provider, receiver, clock = 0;
  const output = { appendLine() {} };
  const vscode = {
    ConfigurationTarget: { Global: 1, Workspace: 2, WorkspaceFolder: 3 },
    Uri: { file: fsPath => ({ fsPath }), joinPath: (...parts) => parts.join('/') },
    workspace: { workspaceFolders: [], visibleTextEditors: [], textDocuments: [], getConfiguration: () => ({
      get: (key, fallback) => overrides[key] ?? settings[key] ?? fallback,
      inspect: key => ({ globalValue: settings[key] || undefined, workspaceValue: overrides[key] }),
      update: async (key, value, target) => { (target === 2 ? overrides : settings)[key] = value; },
    }) },
    window: { visibleTextEditors: [], createOutputChannel: () => output,
      registerWebviewViewProvider: (id, instance) => { provider = instance; return {}; },
      showWarningMessage: async (...args) => { const prompt = { args }; prompts.push(prompt); return new Promise(resolve => prompt.resolve = resolve); },
      showErrorMessage() {}, showInformationMessage: async (...args) => {
        if (args[0].startsWith('Kyrex: Reviewing')) return undefined;
        const prompt = { args }; information.push(prompt); return new Promise(resolve => prompt.resolve = resolve);
      } },
    commands: {
      registerCommand: (name, fn) => { commands.set(name, fn); return {}; },
      executeCommand: async (name, ...args) => commands.get(name)?.(...args),
    },
  };
  const fakeSpawn = (executable, args, options) => {
    const child = new EventEmitter();
    child.options = options; child.frames = []; child.killed = false;
    child.stdout = new EventEmitter(); child.stderr = new EventEmitter();
    child.stdin = { on() {}, writable: true, write: text => child.frames.push(JSON.parse(text)) };
    child.kill = () => { child.killed = true; child.stdin.writable = false; };
    child.emitFrame = payload => child.stdout.emit('data', Buffer.from(JSON.stringify(payload) + '\n'));
    children.push(child); return child;
  };
  const sandbox = {
    module: { exports: {} }, require: name => name === 'vscode' ? vscode : name === 'child_process' ? { spawn: fakeSpawn } : require(name),
    process: { env }, console, Buffer, URL, AbortSignal, fetch: fetchImpl || (async () => { throw Error('No live provider requests in tests'); }),
    setTimeout: fn => { const id = ++clock; timers.set(id, fn); return id; }, clearTimeout: id => timers.delete(id),
  };
  vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, '../dist/extension.js'), 'utf8'), sandbox);
  const context = { extensionPath: '/extension', extensionUri: '/extension', subscriptions: [], secrets: {
    get: async key => secrets.get(key), store: async (key, value) => secrets.set(key, value),
  } };
  sandbox.module.exports.activate(context);
  function view() {
    const webview = { options: {}, html: '', postMessage: msg => messages.push(msg), onDidReceiveMessage: fn => { receiver = fn; return {}; } };
    provider.resolveWebviewView({ webview });
    return webview;
  }
  return { children, messages, prompts, information, settings, secrets, commands, timers, view,
    receive: msg => receiver(msg), dispose: () => sandbox.module.exports.deactivate() };
}
async function setup(initial, env) { const h = harness(initial, env); await tick(); h.webview = h.view(); return h; }
function ui(html) {
  const sent = [];
  const dom = new JSDOM(html, { runScripts: 'dangerously', beforeParse(window) {
    window.acquireVsCodeApi = () => ({ postMessage: msg => sent.push(msg) });
    window.HTMLElement.prototype.scrollTo = function () {};
  } });
  return { dom, sent, doc: dom.window.document,
    send: payload => dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data: payload })),
    frame: payload => dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data: { type: 'engine', payload } })) };
}

test('readiness is replayed to a newly opened sidebar; env key survives empty default', async () => {
  const h = harness({}, { KYREX_API_KEY: 'env-secret' }); await tick();
  assert.equal(h.children[0].options.env.KYREX_API_KEY, 'env-secret');
  h.children[0].emitFrame({ type: 'session_state', model: 'my-model' }); h.view();
  await h.receive({ type: 'ready' });
  assert.equal(h.messages[0].payload.running, true);
  assert.equal(h.messages[1].payload.model, 'my-model'); h.dispose();
});
test('legacy global API key migrates to SecretStorage and is never returned to webview', async () => {
  const h = await setup({ apiKey: 'legacy-secret' });
  assert.equal(h.secrets.get('kyrex.apiKey'), 'legacy-secret'); assert.equal(h.settings.apiKey, undefined);
  await h.receive({ type: 'load_settings' });
  assert.equal(h.messages.at(-1).hasApiKey, true);
  assert.ok(!JSON.stringify(h.messages).includes('legacy-secret')); h.dispose();
});
test('settings apply in one batch with a single restart and custom model ID', async () => {
  const h = await setup();
  await h.receive({ type: 'save_settings', settings: { provider: 'openai', baseUrl: 'https://example.test/v1', model: 'custom/model', apiKey: 'new-secret', apiKeyAction: 'set' } });
  await tick();
  assert.equal(h.children.length, 2); assert.equal(h.children[0].killed, true);
  assert.equal(h.children[1].options.env.KYREX_MODEL, 'custom/model');
  assert.equal(h.children[1].options.env.KYREX_API_KEY, 'new-secret');
  assert.ok(!JSON.stringify(h.messages).includes('new-secret'));
  h.children[0].emit('close', 0);
  h.children[1].emitFrame({ type: 'session_state' });
  assert.equal(h.messages.at(-2).payload.running, true); h.dispose();
});
test('invalid settings do not restart and startup timeout goes offline', async () => {
  const h = await setup();
  await h.receive({ type: 'save_settings', settings: { provider: 'openai', model: '', apiKey: '', apiKeyAction: 'keep', baseUrl: 'file:///tmp/key' } });
  assert.equal(h.messages.at(-1).type, 'settings_error'); assert.equal(h.children.length, 1);
  [...h.timers.values()][0]();
  assert.equal(h.children[0].killed, true); assert.equal(h.messages.at(-1).payload.running, false); h.dispose();
});
test('approvals are correlated, dismissal denies and restart cancels pending approvals', async () => {
  const h = await setup(); const child = h.children[0];
  child.emitFrame({ type: 'confirm_request', id: 'delete-1', value: 'deletion', path: 'a.txt', diff: 'Delete a.txt' }); await tick();
  h.prompts[0].resolve('Approve'); await tick();
  assert.deepEqual(child.frames[0], { type: 'confirm_response', id: 'delete-1', approved: true });
  child.emitFrame({ type: 'confirm_request', id: 'edit-2', value: 'edit', diff: '+new' }); await tick();
  h.prompts[1].resolve(undefined); await tick(); assert.equal(child.frames[1].approved, false);
  child.emitFrame({ type: 'confirm_request', id: 'unknown', value: 'calendar_create' });
  assert.equal(child.frames.at(-1).approved, false);
  child.emitFrame({ type: 'confirm_request', id: 'pending', value: 'deletion' }); await tick();
  await h.receive({ type: 'restart' }); await tick();
  assert.equal(child.frames.at(-1).id, 'pending'); assert.equal(child.frames.at(-1).approved, false);
  h.prompts[2].resolve('Approve'); await tick(); assert.equal(h.children[1].frames.length, 0); h.dispose();
});
test('stop during asynchronous startup cannot create a stray engine', async () => {
  const h = harness(); h.commands.get('kyrex-vscode.stop')(); await tick();
  assert.equal(h.children.length, 0); h.dispose();
});
test('final-only answers render; authoritative finals replace streamed text; interrupts keep partials', async () => {
  const h = await setup(); const u = ui(h.webview.html);
  u.frame({ type: 'chat_done', content: 'Final only', outcome: 'answered', terminal: true });
  assert.equal(u.doc.querySelectorAll('.msg.assistant').length, 1);
  assert.match(u.doc.querySelector('.msg-body').textContent, /Final only/);
  u.frame({ type: 'token', content: 'draft' });
  u.frame({ type: 'chat_done', content: 'Correct final', outcome: 'incomplete', terminal: false });
  assert.match(u.doc.querySelectorAll('.msg-body')[1].textContent, /Correct final/);
  assert.ok(!u.doc.querySelectorAll('.msg-body')[1].textContent.includes('draft'));
  assert.equal(u.doc.getElementById('turn-status').textContent, 'Turn incomplete');
  u.frame({ type: 'token', content: 'partial answer' });
  u.frame({ type: 'chat_done', content: '', outcome: 'interrupted', terminal: false });
  assert.match(u.doc.querySelectorAll('.msg-body')[2].textContent, /partial answer/);
  u.frame({ type: 'chat_done', content: '', outcome: 'provider_error', terminal: false });
  assert.match(u.doc.getElementById('turn-status').textContent, /provider_error/);
  u.dom.window.close(); h.dispose();
});
test('failed tools get failure badges and usage comes from engine rather than stream chunks', async () => {
  const h = await setup(); const u = ui(h.webview.html);
  u.frame({ type: 'tool_start', id: 'failure', name: 'run_command', args: { command: 'false' } });
  u.frame({ type: 'tool_result', id: 'failure', result: { error: 'Timed out' } });
  assert.ok(u.doc.querySelector('[data-tool-id="failure"] .tool-call-status').classList.contains('failed'));
  u.frame({ type: 'token', content: 'chunk' });
  assert.equal(u.doc.getElementById('token-count').textContent, 'Usage unavailable');
  u.frame({ type: 'tui_pause', value: 'usage_stats_silent', files: { prompt_tokens: 1200, completion_tokens: 300, cost: .015 } });
  assert.match(u.doc.getElementById('token-count').textContent, /1,500 tokens/);
  u.dom.window.close(); h.dispose();
});
test('settings are staged while typing; custom model can be applied; offline clears spinner and blocks sends', async () => {
  const h = await setup(); const u = ui(h.webview.html);
  const key = u.doc.getElementById('api-key-input'); key.value = 'draft'; key.dispatchEvent(new u.dom.window.Event('input'));
  u.doc.getElementById('model-input').value = 'custom/model';
  assert.equal(u.sent.filter(msg => msg.type === 'save_settings').length, 0);
  u.doc.getElementById('apply-settings-btn').click();
  assert.equal(u.sent.at(-1).settings.model, 'custom/model'); assert.equal(u.sent.at(-1).settings.apiKey, 'draft');
  u.send({ type: 'engine_status', payload: { running: true } });
  u.doc.getElementById('prompt').value = 'hello'; u.doc.getElementById('send-btn').click();
  assert.ok(u.doc.querySelector('.sending'));
  u.send({ type: 'engine_status', payload: { running: false } });
  assert.equal(u.doc.querySelector('.sending'), null); assert.equal(u.doc.getElementById('send-btn').disabled, true);
  assert.ok(u.doc.getElementById('status-dot').classList.contains('offline'));
  assert.match(u.doc.getElementById('turn-status').textContent, /disconnected/);
  u.dom.window.close(); h.dispose();
});

test('saved catalogue URLs and Anthropic headers are correct without duplicated v1', async () => {
  const requests = [];
  const h = harness({ provider: 'anthropic', baseUrl: 'https://api.example.test/v1' }, { KYREX_API_KEY: 'env-key' }, async (url, options) => {
    requests.push({ url, options }); return { ok: true, json: async () => ({ data: [{ id: 'my-model' }] }) };
  });
  await tick(); h.view(); await h.receive({ type: 'fetch_models' });
  assert.equal(requests[0].url, 'https://api.example.test/v1/models');
  assert.equal(requests[0].options.headers['x-api-key'], 'env-key');
  assert.equal(requests[0].options.headers.Authorization, undefined);
  assert.equal(h.messages.at(-1).models[0], 'my-model'); h.dispose();
});
test('old native edit dialog cannot accept a new process edit after restart', async () => {
  const h = await setup(); const old = h.children[0];
  old.emitFrame({ type: 'propose_edit', editId: 'old-dialog', filePath: path.join(require('node:os').tmpdir(), 'kyrex-test-target.txt'), content: 'first' });
  await tick();
  await h.receive({ type: 'restart' }); await tick(); const fresh = h.children[1];
  fresh.emitFrame({ type: 'propose_edit', editId: 'new-dialog', filePath: path.join(require('node:os').tmpdir(), 'kyrex-test-target.txt'), content: 'second' });
  await tick();
  h.information[0].resolve('Accept'); await tick();
  assert.equal(fresh.frames.length, 0);
  h.information[1].resolve('Accept'); await tick();
  assert.deepEqual(fresh.frames[0], { type: 'edit_decision', editId: 'new-dialog', accepted: true }); h.dispose();
});

test('applying settings updates existing workspace overrides and replaces legacy workspace key', async () => {
  const h = await setup({ workspaceOverrides: { model: 'workspace-old', baseUrl: 'https://old.test/v1', apiKey: 'workspace-key' } });
  assert.equal(h.children[0].options.env.KYREX_API_KEY, 'workspace-key');
  await h.receive({ type: 'save_settings', settings: { provider: 'openai', baseUrl: 'https://new.test/v1', model: 'new-model', apiKey: 'saved-new', apiKeyAction: 'set' } });
  await tick();
  assert.equal(h.children[1].options.env.KYREX_MODEL, 'new-model');
  assert.equal(h.children[1].options.env.KYREX_BASE_URL, 'https://new.test/v1');
  assert.equal(h.children[1].options.env.KYREX_API_KEY, 'saved-new'); h.dispose();
});
test('bridge telemetry without IDs updates distinct tool cards in order', async () => {
  const h = await setup(); const u = ui(h.webview.html);
  u.frame({ type: 'tool_start', name: 'first_tool', args: {} });
  u.frame({ type: 'tool_start', name: 'second_tool', args: {} });
  u.frame({ type: 'tool_result', name: 'first_tool', result: { error: 'failed' } });
  u.frame({ type: 'tool_result', name: 'second_tool', result: { ok: true } });
  const badges = [...u.doc.querySelectorAll('.tool-call-status')];
  assert.ok(badges[0].classList.contains('failed')); assert.ok(badges[1].classList.contains('success'));
  u.dom.window.close(); h.dispose();
});
