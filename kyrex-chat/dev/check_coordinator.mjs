// dev/check_coordinator.mjs — deterministic UI check for the Coordinator
// ("Chief of Staff") configuration surface in Bot Settings.
//
// Renders the REAL BotSettings component under jsdom against a mocked /api and
// asserts that enabling coordination is:
//   * a visible, owner-scoped control ("Configure as Coordinator") on an owned
//     Bot, kept SEPARATE from "Configure as Developer Bot";
//   * NOT offered for a Bot the user does not own (a legacy Bot only offers a
//     claim) — the owner restriction is enforced in the UI as well as the API;
//   * gated behind an explicit confirmation that states the coordinator cannot
//     approve another Bot's actions or inherit its credentials/browser/provider
//     keys/Rift/write permissions;
//   * sent as the single named preset ("coordinator") — never a policy, never a
//     browser allowlist, never a write grant;
//   * reflected afterwards as a visible Coordinator badge, refreshed from the
//     server's own roster rather than guessed.
//
// Run: node --import ./dev/jsx-loader-register.mjs dev/check_coordinator.mjs

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
// The coordinator preset: the coordination host op + safe reads; every unsafe
// op (write/browser/etc.) is denied. Mirrors serve.effective_permissions for
// COORDINATOR_PRESET.
const COORD_PERMS = {
  'fs:read': 0, 'repo:read': 0, 'bot:delegate': 0,
  'fs:write': 'deny', 'repo:pr': 'deny', 'repo:push': 'deny',
  'fs:delete': 'deny', 'browser:navigate': 'deny', 'browser:click': 'deny',
};
const DEV_PERMS = { 'fs:read': 0, 'repo:read': 0, 'fs:write': 1, 'repo:pr': 1 };
const PRESETS = {
  presets: [
    {
      id: 'developer', label: 'Developer Bot',
      policy: { 'fs:read': 0, 'repo:read': 0, 'fs:write': 1, 'repo:pr': 1 },
      permissions: DEV_PERMS,
    },
    {
      id: 'coordinator', label: 'Chief of Staff (coordinator)',
      policy: { 'fs:read': 0, 'repo:read': 0, 'bot:delegate': 0 },
      permissions: COORD_PERMS,
    },
  ],
};
const PROFILES = {
  profiles: [{
    id: 'prof-a', name: 'Prof A', provider: 'openai',
    base_url: 'https://api.example/v1', models: ['m1', 'm2'],
    has_api_key: true, api_key_last4: '1234', header_names: [],
  }],
};
const WORKSPACES = { workspaces: [{ id: 'ws1', name: 'ws1', available: true }] };

// OWNED bot (manageable) + a legacy (ownerless) bot the user does not own.
let roster = [
  {
    id: 'chief', name: 'Chief', status: 'running', model: 'm1',
    available: true, manageable: true, claimable: false, coordinator: false,
  },
  {
    id: 'legacybot', name: 'Legacy', status: 'stopped', model: 'm0',
    available: true, manageable: false, claimable: true, coordinator: false,
  },
];
const configurePosts = [];

globalThis.fetch = async (input, init = {}) => {
  const url = String(input && input.url ? input.url : input);
  const method = init.method || 'GET';
  if (url.includes('/api/bots/presets')) return Response.json(PRESETS);
  if (url.includes('/api/chat/provider-profiles')) return Response.json(PROFILES);
  if (url.includes('/api/chat/workspaces')) return Response.json(WORKSPACES);

  const configure = url.match(/\/api\/bots\/([^/]+)\/configure$/);
  if (configure && method === 'POST') {
    const id = decodeURIComponent(configure[1]);
    const body = JSON.parse(init.body || '{}');
    configurePosts.push({ id, body });
    if (body.preset === 'coordinator') {
      roster = roster.map((b) => (b.id === id ? { ...b, coordinator: true } : b));
    }
    const target = roster.find((b) => b.id === id) || { id };
    return Response.json({
      ...target,
      coordinator: body.preset === 'coordinator',
      writable: false,
      policy: (PRESETS.presets.find((p) => p.id === body.preset) || {}).policy,
      permissions: body.preset === 'coordinator' ? COORD_PERMS : DEV_PERMS,
    });
  }
  // Any other configure call is unexpected in this check.
  if (/\/api\/bots\/[^/]+\/configure$/.test(url)) {
    return Response.json({ detail: 'unexpected configure call' }, { status: 400 });
  }

  if (/\/api\/bots$/.test(url) && method === 'GET') {
    return Response.json({ bots: roster });
  }
  return Response.json({}, { status: 404 });
};

// ── boot React ──────────────────────────────────────────────────────
const React = (await import('react')).default;
const { createRoot } = await import('react-dom/client');
const { default: BotSettings } = await import('../src/components/BotSettings.jsx');

// A host that stands in for App: it holds the roster and refetches it when the
// component signals a change (exactly what refreshBots does in App.jsx). The
// badge therefore comes from the SERVER's roster, not local optimism.
function Host() {
  const [bots, setBots] = React.useState([]);
  const refresh = () => fetch('/api/bots').then((r) => r.json()).then((d) => setBots(d.bots));
  React.useEffect(() => { refresh(); }, []);
  return React.createElement(BotSettings, {
    bots,
    onClose: () => {},
    onChanged: refresh,
  });
}

const root = createRoot(document.getElementById('root'));
root.render(React.createElement(Host));
await sleep(400);

// ── helpers ─────────────────────────────────────────────────────────
function buttons() { return [...document.querySelectorAll('button')]; }
function byText(text) { return buttons().find((b) => b.textContent.trim() === text); }
function countByText(text) { return buttons().filter((b) => b.textContent.trim() === text).length; }
function permValue(op) {
  const row = [...document.querySelectorAll('.perm-row')]
    .find((r) => (r.querySelector('.perm-op') || {}).textContent?.trim() === op);
  return row ? (row.querySelector('.perm-val') || {}).textContent?.trim() : null;
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

// ── 1. the coordinator control is visible + separate from Developer ─
check('coordinator control is visible for an owned Bot',
  Boolean(byText('Configure as Coordinator')));
check('developer control remains a distinct action',
  Boolean(byText('Configure as Developer Bot'))
  && byText('Configure as Coordinator') !== byText('Configure as Developer Bot'));

// ── 2. owner restriction: a non-owned Bot is NOT offered coordination ─
check('a non-owned (legacy) Bot is offered only a claim, not coordination',
  Boolean(byText('Claim legacy Bot')) && countByText('Configure as Coordinator') === 1,
  `coordinatorButtons=${countByText('Configure as Coordinator')}`);

// ── 3. explicit confirmation with the required explanation ──────────
byText('Configure as Coordinator').click();
const dialog = await waitFor(() =>
  document.querySelector('.bot-confirm[role="dialog"][aria-label="Confirm Coordinator Bot"]'));
check('an explicit confirmation dialog is required', Boolean(dialog));
const dtext = dialog ? dialog.textContent : '';
check('dialog explains it cannot approve another Bot or inherit credentials/browser/provider keys/Rift/write',
  /cannot approve another Bot/i.test(dtext)
  && /credentials, browser sessions, provider keys, Rift, or write/i.test(dtext),
  dtext.slice(0, 120));
check('dialog states it grants no write/browser/mail/cal/delete/push capability',
  /no\s+filesystem write, PR, browser, mail, calendar, delete, or push/i.test(dtext));

// ── 4. the effective-permission preview proves NO write/browser ─────
check('confirmation shows fs:write as denied', permValue('fs:write') === 'denied',
  String(permValue('fs:write')));
check('confirmation shows browser:navigate as denied',
  permValue('browser:navigate') === 'denied', String(permValue('browser:navigate')));
check('confirmation shows bot:delegate as allowed',
  (permValue('bot:delegate') || '').includes('allowed'), String(permValue('bot:delegate')));

// ── 5. confirming sends ONLY the named preset — no policy/write ─────
byText('Enable coordination').click();
await sleep(200);
check('exactly one configure POST was made', configurePosts.length === 1,
  `count=${configurePosts.length}`);
const post = configurePosts[0] || {};
check('POST targets the Bot\'s existing configure endpoint',
  post.id === 'chief', String(post.id));
check('POST body carries only the coordinator preset',
  post.body.preset === 'coordinator' && Object.keys(post.body).length === 1,
  JSON.stringify(post.body));
check('POST carries NO explicit policy, browser allowlist, or write grant',
  !('policy' in post.body) && !('browser_allowlist' in post.body),
  JSON.stringify(post.body));

// ── 6. roster refresh shows a visible Coordinator badge ─────────────
const badge = await waitFor(() =>
  document.querySelector('.bot-coordinator-tag'));
check('a visible Coordinator badge appears after enabling',
  Boolean(badge) && /coordinator/i.test(badge.textContent));
check('the control now reads "Reconfigure coordination"',
  Boolean(byText('Reconfigure coordination')));
check('the developer control is untouched (still available)',
  Boolean(byText('Configure as Developer Bot')));

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
