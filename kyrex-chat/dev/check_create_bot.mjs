// dev/check_create_bot.mjs — deterministic UI check for the Create Bot flow.
//
// Renders the REAL BotSettings component under jsdom against a mocked /api,
// drives the simplified (Grok-style) Create Bot form, and asserts:
//   * a visible "Create Bot" action exists in Bot Settings;
//   * "Configure as Developer Bot" and "Configure LLM" remain SEPARATE actions;
//   * the basic form is just name + role + model, with advanced controls
//     (id/workspace/status/capability/allowlist) hidden until "Advanced";
//   * the model defaults to the user's configured provider profile;
//   * submitting sends ONE POST /api/bots with the simplified body — no id
//     (the server generates it) and no allowlist (browser denied by default);
//   * a second, advanced submit carries id/preset/allowlist;
//   * the newly created Bot appears in the owner's roster and the form closes;
//   * a server validation error (400) is surfaced verbatim.
//
// Run: node --import ./dev/jsx-loader-register.mjs dev/check_create_bot.mjs

import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  url: 'http://localhost/',
  pretendToBeVisual: true,
});
const { window } = dom;
globalThis.window = window;
globalThis.document = window.document;
Object.defineProperty(globalThis, 'navigator', { value: window.navigator, configurable: true });
globalThis.Node = window.Node;
Object.defineProperty(globalThis, 'localStorage', { value: window.localStorage, configurable: true });
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
window.IS_REACT_ACT_ENVIRONMENT = true;
window.matchMedia = () => ({
  matches: false, addListener() {}, removeListener() {},
  addEventListener() {}, removeEventListener() {},
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── mocked backend ──────────────────────────────────────────────────
const PRESETS = {
  presets: [{
    id: 'developer', label: 'Developer Bot',
    policy: { 'fs:read': 0, 'repo:read': 0, 'fs:write': 1, 'repo:pr': 1 },
    permissions: { 'fs:read': 0, 'repo:read': 0, 'fs:write': 1, 'repo:pr': 1 },
  }],
};
const PROFILES = {
  profiles: [{
    id: 'prof-a', name: 'Prof A', provider: 'openai',
    base_url: 'https://api.example/v1', models: ['m1', 'm2'],
    has_api_key: true, api_key_last4: '1234', header_names: [],
  }],
};
const WORKSPACES = { workspaces: [{ id: 'ws1', name: 'ws1', available: true }] };
const CREATED_BOT = {
  id: 'new-bot', name: 'New Bot', status: 'stopped', model: 'm1',
  available: true, manageable: true, claimable: false,
  provider_profile_id: 'prof-a', provider: { configured: true, profile: null, model: 'm1' },
};

let botPosts = [];
globalThis.fetch = async (input, init = {}) => {
  const url = String(input && input.url ? input.url : input);
  const method = init.method || 'GET';
  if (url.includes('/api/bots/presets')) return Response.json(PRESETS);
  if (url.includes('/api/chat/provider-profiles')) return Response.json(PROFILES);
  if (url.includes('/api/chat/workspaces')) return Response.json(WORKSPACES);
  if (/\/api\/bots$/.test(url) && method === 'POST') {
    const body = JSON.parse(init.body || '{}');
    botPosts.push(body);
    if (body.name === 'Bad') {
      return Response.json({ detail: 'Bot name is reserved' }, { status: 400 });
    }
    return Response.json(CREATED_BOT);
  }
  if (url.includes('/api/bots') && method === 'GET') {
    return Response.json({ bots: [SEED_BOT] });
  }
  return Response.json({}, { status: 404 });
};

const SEED_BOT = {
  id: 'existing', name: 'Existing Bot', status: 'stopped', model: 'm0',
  available: true, manageable: true, claimable: false,
};

// ── boot React ──────────────────────────────────────────────────────
const React = (await import('react')).default;
const { createRoot } = await import('react-dom/client');
const { default: BotSettings } = await import('../src/components/BotSettings.jsx');

// A host that stands in for App: it holds the roster and refreshes it when the
// component signals a change (exactly what refreshBots does in App.jsx).
function Host() {
  const [bots, setBots] = React.useState([SEED_BOT]);
  return React.createElement(BotSettings, {
    bots,
    onClose: () => {},
    onChanged: () => setBots((prev) =>
      prev.some((b) => b.id === CREATED_BOT.id) ? prev : [...prev, CREATED_BOT]),
  });
}

const root = createRoot(document.getElementById('root'));
root.render(React.createElement(Host));
await sleep(300);

// ── helpers ─────────────────────────────────────────────────────────
function buttons() { return [...document.querySelectorAll('button')]; }
function byText(text) { return buttons().find((b) => b.textContent.trim() === text); }
function setValue(el, value) {
  const desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
  desc.set.call(el, value);
  el.dispatchEvent(new window.Event('input', { bubbles: true }));
  el.dispatchEvent(new window.Event('change', { bubbles: true }));
}
async function waitFor(fn, ms = 2000) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    const v = fn();
    if (v) return v;
    await sleep(20);
  }
  return null;
}

let passed = 0;
let failed = 0;
function check(name, ok, detail = '') {
  const line = `${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`;
  if (ok) passed += 1; else failed += 1;
  console.log(line);
}

// ── 1. visible Create Bot action + separate configure actions ───────
check('"Create Bot" action is visible in Bot Settings', Boolean(byText('Create Bot')));
check('"Configure as Developer Bot" action is present', Boolean(byText('Configure as Developer Bot')));
check('"Configure LLM" action is present', Boolean(byText('Configure LLM')));
check('configure actions are distinct buttons',
  byText('Configure as Developer Bot') !== byText('Configure LLM'));

// ── 2. open the form: basic fields only, advanced hidden ────────────
byText('Create Bot').click();
const dialog = await waitFor(() =>
  document.querySelector('.bot-create[role="dialog"]'));
check('Create Bot form opens', Boolean(dialog));
check('basic form exposes name + role + model',
  Boolean(document.getElementById('create-bot-name'))
  && Boolean(document.getElementById('create-bot-prompt'))
  && Boolean(document.getElementById('create-bot-model')));
check('advanced fields are hidden until Advanced is opened',
  !document.getElementById('create-bot-id')
  && !document.getElementById('create-bot-workspace')
  && !document.getElementById('create-bot-status')
  && !document.getElementById('create-bot-preset')
  && !document.getElementById('create-bot-allowlist'));
check('model defaults to the configured profile model',
  (document.getElementById('create-bot-model') || {}).value === 'm1',
  String((document.getElementById('create-bot-model') || {}).value));

setValue(document.getElementById('create-bot-name'), 'New Bot');
setValue(document.getElementById('create-bot-prompt'), 'You are New Bot.');
await sleep(50);

const submit = dialog.querySelector('.send-btn');
check('submit is enabled with just a name (+ prompt + defaulted model)', !submit.disabled);
submit.click();
await sleep(200);

// ── 3. the request body matches the simplified create contract ──────
check('exactly one POST /api/bots was made', botPosts.length === 1, `count=${botPosts.length}`);
const body = botPosts[0] || {};
check('body carries name + role + defaulted model',
  body.name === 'New Bot' && body.role === 'You are New Bot.' && body.model === 'm1',
  JSON.stringify(body));
check('body carries the defaulted provider profile',
  body.provider_profile_id === 'prof-a', String(body.provider_profile_id));
check('body omits an id (server generates it)', !('id' in body), JSON.stringify(body));
check('body carries initial status (stopped)', body.status === 'stopped', String(body.status));
check('browser access is not enabled by default',
  !body.browser_allowlist || body.browser_allowlist.length === 0,
  JSON.stringify(body.browser_allowlist));

// ── 4. roster refresh + form closed ─────────────────────────────────
check('form closes after a successful create',
  !document.querySelector('.bot-create[role="dialog"]'));
const rosterText = document.querySelector('.provider-list')?.textContent || '';
check('newly created Bot appears in the owner roster', rosterText.includes('New Bot'));

// ── 5. advanced section exposes the separate controls + sends them ───
byText('Create Bot').click();
await waitFor(() => document.querySelector('.bot-create[role="dialog"]'));
byText('Advanced').click();
await sleep(50);
check('advanced section exposes id + workspace + status + capability + allowlist',
  Boolean(document.getElementById('create-bot-id'))
  && Boolean(document.getElementById('create-bot-workspace'))
  && Boolean(document.getElementById('create-bot-status'))
  && Boolean(document.getElementById('create-bot-preset'))
  && Boolean(document.getElementById('create-bot-allowlist')));
setValue(document.getElementById('create-bot-name'), 'Advanced Bot');
setValue(document.getElementById('create-bot-id'), 'advanced-bot');
setValue(document.getElementById('create-bot-prompt'), 'Advanced.');
setValue(document.getElementById('create-bot-preset'), 'developer');
setValue(document.getElementById('create-bot-allowlist'), 'example.com, docs.example.com');
await sleep(50);
document.querySelector('.bot-create .send-btn').click();
await sleep(200);
const advBody = botPosts[botPosts.length - 1] || {};
check('advanced body carries explicit id, preset, and parsed allowlist',
  advBody.id === 'advanced-bot' && advBody.preset === 'developer'
  && Array.isArray(advBody.browser_allowlist)
  && advBody.browser_allowlist.join(',') === 'example.com,docs.example.com',
  JSON.stringify(advBody));

// ── 6. validation error is surfaced verbatim ────────────────────────
byText('Create Bot').click();
await waitFor(() => document.querySelector('.bot-create[role="dialog"]'));
setValue(document.getElementById('create-bot-name'), 'Bad');
await sleep(50);
document.querySelector('.bot-create .send-btn').click();
const errEl = await waitFor(() => document.querySelector('.message-error'));
check('server validation error is shown clearly',
  Boolean(errEl) && /reserved/i.test(errEl.textContent),
  errEl ? errEl.textContent : '(no error element)');

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
