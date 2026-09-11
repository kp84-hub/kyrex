// dev/check_user_bubble_stability.mjs — deterministic runtime check.
//
// Renders the REAL App (real useChat + MessageList + Message) under jsdom,
// drives a send against a mocked SSE backend, and instruments the DOM:
//   * a MutationObserver records every removal/insertion of the user bubble
//     element during assistant SSE updates,
//   * the user bubble element identity is compared before/after each update.
// Exits 0 when the user bubble is never remounted/replaced, 1 otherwise.
// Also logs message ids/roles at every observed render of the list.

import { JSDOM } from 'jsdom';
import fs from 'node:fs';

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  url: 'http://localhost/',
  pretendToBeVisual: true,
});
const { window } = dom;
globalThis.window = window;
globalThis.document = window.document;
Object.defineProperty(globalThis, 'navigator', { value: window.navigator, configurable: true });
globalThis.Node = window.Node;
// The app's readActive()/persistActive() bind the bare `localStorage` global —
// point Node's global at jsdom's implementation so they share one storage.
Object.defineProperty(globalThis, 'localStorage', { value: window.localStorage, configurable: true });
globalThis.MutationObserver = window.MutationObserver;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
window.IS_REACT_ACT_ENVIRONMENT = true;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// jsdom Element.prototype.scroll* shims
window.Element.prototype.scrollTo = function () {};
window.Element.prototype.scrollBy = function () {};
for (const k of ['immel', 'scrollTop', 'scrollHeight']) { /* noop */ }
Object.defineProperty(window.Element.prototype, 'scrollTop', { get() { return 0; }, set() {} });
Object.defineProperty(window.Element.prototype, 'scrollHeight', { get() { return 1000; }, set() {} });
Object.defineProperty(window.Element.prototype, 'clientHeight', { get() { return 500; }, set() {} });
Object.defineProperty(window.Element.prototype, 'clientWidth', { get() { return 800; }, set() {} });
window.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} });

// ── mocked backend ──────────────────────────────────────────────────
const PROVIDERS = { providers: [{ id: 'omen', label: 'Omen', models: ['o1', 'o2'] }] };
const CONVERSATIONS = { conversations: [{ conversation_id: 'c1', title: 'c1', message_count: 0 }] };
const BOT_TURN_PROMISE = { resolve: null };

const sse = (frames) =>
  new Response(
    new ReadableStream({
      start(c) {
        for (const f of frames) c.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(f)}\n\n`));
        c.close();
      },
    }),
    { status: 200, headers: { 'content-type': 'text/event-stream' } }
  );

let chatRequests = 0;
let getConvRequests = 0;
// Refetch budget: StrictMode re-mounts effects once (2 restore fetches);
// anything well beyond that is the infinite bootstrap/refetch loop.
const MAX_BOOT_REFETCHES = Number(process.env.MAX_BOOT_REFETCHES ?? 3);
globalThis.fetch = async (input, init = {}) => {
  const url = String(input && input.url ? input.url : input);
  const method = init.method || 'GET';
  if (process.env.MOCK_TRACE) console.log(`[mock] ${method} ${url}`);
  if (url.includes('/api/chat/status')) return Response.json({ available: true });
  if (url.includes('/api/chat/providers')) return Response.json(PROVIDERS);
  if (url.includes('/api/chat/workspaces')) return Response.json({ workspaces: [] });
  if (url.includes('/api/bots')) return Response.json({ bots: [] });
  if (/\/api\/conversations\/c1$/.test(url) && method === 'GET') {
    getConvRequests += 1;
    return Response.json({
      conversation_id: 'c1',
      messages: [
        { id: 'u1', role: 'user', content: 'seeded user message', created_at: '2026-01-01T00:00:00Z' },
        { id: 'a1', role: 'assistant', content: 'seeded assistant reply', created_at: '2026-01-01T00:00:01Z' },
      ],
    });
  }
  if (url.includes('/api/conversations') && method === 'GET') return Response.json(CONVERSATIONS);
  if (url.includes('/api/conversations') && method === 'POST')
    return Response.json({ conversation_id: 'c1' });
  if (url.includes('/api/chat') && method === 'POST') {
    chatRequests += 1;
    const N = 40; // 40 sequential deltas streamed slowly
    const frames = [{ type: 'conversation', conversation_id: 'c1' }];
    for (let i = 0; i < N; i++) frames.push({ type: 'delta', content: 'x' });
    frames.push({ type: 'done', content: 'x'.repeat(N), conversation_id: 'c1' });
    // Space deltas across chunks so updates land in separate renders.
    const SLOW_SSE = new Response(
      new ReadableStream({
        async start(c) {
          for (const f of frames) {
            c.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(f)}\n\n`));
            await new Promise((r) => setTimeout(r, 5));
          }
          c.close();
          BOT_TURN_PROMISE.resolve?.();
        },
      }),
      { status: 200, headers: { 'content-type': 'text/event-stream' } }
    );
    return SLOW_SSE;
  }
  if (url.includes('/api/chat/cancel')) return Response.json({ cancelled: false });
  return Response.json({}, { status: 404 });
};

// ── boot React app (StrictMode, exactly like src/main.jsx in dev) ──
const React = (await import('react')).default;
const { createRoot } = await import('react-dom/client');
globalThis.React = React;
const { default: App } = await import('../src/App.jsx');

if (process.env.RESTORE_CHAT === '1') window.localStorage.setItem('kyrex-chat.activeConversationId', 'c1');

process.on('unhandledRejection', (e) => console.log('UNHANDLED:', e?.message, e?.stack?.split('\n')[1]));
const root = createRoot(document.getElementById('root'));
await root.render(
  React.createElement(React.StrictMode, null, React.createElement(App))
);
await sleep(300);
console.log('stored activeId after boot:', window.localStorage.getItem('kyrex-chat.activeConversationId'));

// ── instrumentation ─────────────────────────────────────────────────
const findUserBubble = () => {
  const bubbles = [...document.querySelectorAll('.message-user .message-bubble')];
  return bubbles.length ? bubbles[0] : null;
};

const mutations = [];
const mo = new MutationObserver((recs) => {
  for (const r of recs) {
    for (const n of r.removedNodes) {
      if (n.classList?.contains('message-user') || n.querySelector?.('.message-user')) {
        mutations.push(reasonOf(r, n));
      }
    }
    for (const n of r.addedNodes) {
      if (n.classList?.contains('message-user') && !firstInsertSeen) {
        firstInsertSeen = true;
      } else if (n.classList?.contains('message-user')) {
        mutations.push('user bubble RE-INSERTED (remount?)');
      }
    }
  }
});
let firstInsertSeen = false;
function reasonOf(record, node) {
  const parentRole = record.target?.className || '';
  const viaReact = record.target === document.getElementById('root') ? 'root-level' : `parent .${parentRole}`;
  return `user bubble REMOVED (${viaReact})`;
}
mo.observe(document.getElementById('root'), { childList: true, subtree: true });

let replacedCount = 0;
const replacedEvents = [];
const identityByNode = new Map(); // node -> text
const lastNodeByText = new Map(); // text -> most recent node
const idsSeen = [];
let lastIds = '';

// Custom render sampler: patch MessageList via its module? Instead sample DOM.
function sampleIds() {
  const kids = [...document.querySelectorAll('.message')].map((el) =>
    el.querySelector('.message-bubble') ? 'user:' + el.textContent.slice(0, 12) : 'asst'
  );
  const sig = kids.join('|');
  if (sig !== lastIds) {
    idsSeen.push(sig);
    lastIds = sig;
  }
}

// ── drive: type + click Send ────────────────────────────────────────
// Wait for boot (providers/conversations fetches settle) before typing.
let ta = null;
for (let i = 0; i < 200; i++) {
  ta = document.querySelector('.composer-input');
  if (ta) break;
  await sleep(25);
}
console.log('composer found:', ta?.tagName);
const val = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
val.call(ta, 'hello kyrex');
ta.dispatchEvent(new window.Event('input', { bubbles: true }));
await sleep(50);

let userNode = findUserBubble();
const t0 = Date.now();
let sendBtn = null;
for (let i = 0; i < 100 && !sendBtn; i++) {
  sendBtn = [...document.querySelectorAll('.send-btn')].find((b) => b.textContent === 'Send');
  if (!sendBtn) await sleep(25);
}
sendBtn.click();
await sleep(120); // optimistic user bubble should now exist

// Streaming runs ~40*5ms=200ms; poll during it, then continue to the end.
const deadline = Date.now() + 15000;
let checks = 0;
// Reset the baseline right before the send so boot-phase restore churn is
// reported separately from the send-phase churn.
identityByNode.clear();
lastNodeByText.clear();

while (Date.now() < deadline) {
  await sleep(10);
  checks++;
  sampleIds();
  for (const bubble of document.querySelectorAll('.message-user .message-bubble')) {
    const text = bubble.textContent;
    const known = identityByNode.get(bubble);
    if (!known) {
      identityByNode.set(bubble, text);
      const prevNode = lastNodeByText.get(text);
      if (prevNode && prevNode !== bubble) {
        replacedCount++;
        replacedEvents.push(
          `[t=${Date.now() - t0}ms] "${text}" bubble remounted: node #${[...identityByNode.entries()]
            .filter(([, t]) => t === text)
            .indexOf(bubble)} replaced`
        );
        lastNodeByText.set(text, bubble);
      }
      if (!prevNode) lastNodeByText.set(text, bubble);
    }
  }
  const asst = document.querySelector('.message-assistant .streaming-text');
  if (asst && asst.textContent.length >= 40) break; // done frame rendered
}
await sleep(300); // let terminal rendering settle

// ── report ──────────────────────────────────────────────────────────
console.log('--- distinct message-list render signatures ---');
for (const s of idsSeen) console.log(' ', s);
console.log('--- DOM mutation findings ---');
const unique = [...new Set(mutations)];
if (unique.length === 0) console.log('  no user-bubble removal/reinsertion events');
else unique.forEach((m) => console.log(' ', m));
console.log('--- remount findings (text identity) ---');
if (replacedEvents.length === 0) console.log('  no remounted user bubbles');
else replacedEvents.forEach((m) => console.log(' ', m));
console.log('--- summary ---');
console.log(`chat POST requests: ${chatRequests}`);
console.log(`getConversation requests: ${getConvRequests}`);
console.log(`user bubble node replaced during stream: ${replacedCount} of ${checks} samples`);
const finalBubbles = document.querySelectorAll('.message-user .message-bubble').length;
console.log(`final .message-user bubbles in DOM: ${finalBubbles}`);
// StrictMode re-mounts effects once, so the restore refetch may legitimately
// run twice; anything beyond that is the refetch loop. In the restore
// scenario two user bubbles is correct (persisted turn + new turn).
const expectedBubbles = process.env.RESTORE_CHAT === '1' ? 2 : 1;
const maxBootRefetches = MAX_BOOT_REFETCHES;
const ok =
  chatRequests === 1 &&
  replacedCount === 0 &&
  mutations.length === 0 &&
  finalBubbles === expectedBubbles &&
  getConvRequests <= maxBootRefetches;
console.log(`getConversation refetch budget: ${getConvRequests}/${maxBootRefetches}`);
console.log(ok ? 'RESULT: PASS' : 'RESULT: FAIL');
process.exit(ok ? 0 : 1);
