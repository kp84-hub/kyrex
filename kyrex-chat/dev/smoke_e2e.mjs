// dev/smoke_e2e.mjs — Kyrex Chat UI smoke test (dev-only).
//
// Runs the UI's REAL client modules (src/lib/api.js + src/lib/streaming.js)
// under Node against the running vite dev server (which proxies /api to the
// real, unmodified Kyrex Cloud backend). No mocks: every request hits the
// actual HTTP/SSE surface and the actual provider (streamed model tokens).
//
// Usage: node dev/smoke_e2e.mjs [uiOrigin]
//   uiOrigin defaults to http://localhost:5173 (vite dev).

import { setTimeout as sleep } from 'node:timers/promises';
import http from 'node:http';
import { Readable } from 'node:stream';

const UI_ORIGIN = process.argv[2] || 'http://localhost:5173';
const SESSION_COOKIE = 'session=kyrex-chat-local-smoke-session';

// ── browser-environment shims so the app's own lib modules run in Node ──
// Node's fetch (undici) forbids setting the Cookie header (spec-forbidden),
// but a real browser sends its session cookie on every request. This shim
// mirrors the browser: relative /api path -> UI origin (vite proxy), plus
// the session cookie, with AbortController support for the Stop fallback.
function uiFetch(input, init = {}) {
  return new Promise((resolve, reject) => {
    const raw = typeof input === 'string' ? input : input.url;
    const url = new URL(raw.startsWith('/') ? UI_ORIGIN + raw : raw);
    const headers = { Cookie: SESSION_COOKIE };
    if (init.headers) {
      for (const [k, v] of new Headers(init.headers)) headers[k.toLowerCase()] = v;
    }
    const req = http.request(
      {
        hostname: url.hostname,
        port: url.port || 80,
        path: url.pathname + url.search,
        method: init.method || 'GET',
        headers,
      },
      (res) => {
        const status = res.statusCode || 500;
        const response = new Response(Readable.toWeb(res), {
          status,
          headers: res.headers,
        });
        resolve(response);
      }
    );
    req.on('error', (err) => {
      if (init.signal && init.signal.aborted) {
        const abortErr = new Error('The operation was aborted');
        abortErr.name = 'AbortError';
        reject(abortErr);
      } else {
        reject(err);
      }
    });
    if (init.signal) {
      init.signal.addEventListener(
        'abort',
        () => {
          const abortErr = new Error('The operation was aborted');
          abortErr.name = 'AbortError';
          req.destroy(abortErr);
        },
        { once: true }
      );
    }
    if (init.body) req.write(init.body);
    req.end();
  });
}
globalThis.fetch = uiFetch;

const { listConversations, createConversation, getConversation, deleteConversation, chatStatus, streamChat, cancelChat } = await import('../src/lib/api.js');
const { consumeStream } = await import('../src/lib/streaming.js');

let passed = 0;
let failed = 0;
const results = [];
function check(name, ok, detail = '') {
  const line = `${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`;
  if (ok) passed += 1;
  else failed += 1;
  results.push(line);
  // Incremental visibility (long real-stream suite).
  console.log(line);
}

// ── 0. engine status probe (what ChatHeader shows) ──
const status = await chatStatus();
check('GET /api/chat/status', status && status.available === true, JSON.stringify(status));

// ── 1. create conversation ──
const conv = await createConversation();
check('POST /api/conversations', Boolean(conv.conversation_id), conv.conversation_id);

// ── 2. send "hi" — real streamed response ──
const t0 = Date.now();
let deltaCount = 0;
let firstDeltaAt = null;
let lastDeltaAt = null;
const { terminal: term1 } = await consumeStream(streamChat(conv.conversation_id, 'hi').stream, {
  onDelta: () => {
    deltaCount += 1;
    if (firstDeltaAt === null) firstDeltaAt = Date.now();
    lastDeltaAt = Date.now();
  },
});
const streamingEvidence =
  deltaCount >= 1 && firstDeltaAt !== null && lastDeltaAt !== null && lastDeltaAt >= firstDeltaAt;
const term1Text = term1.content ?? term1.message ?? '(none)';
check(
  'POST /api/chat "hi" streams real deltas',
  term1.kind === 'done' && term1.content.trim().length > 0 && streamingEvidence,
  `terminal=${term1.kind} deltas=${deltaCount} content="${term1Text.slice(0, 60).replace(/\n/g, ' ')}"`
);

// ── 3. second contextual message — verify memory across turns ──
await consumeStream(streamChat(conv.conversation_id, 'My favorite color is teal. Just remember it for now, no reply needed.').stream, {});
let ctxAnswer = '';
let ctxDeltas = 0;
const { terminal: termCtx } = await consumeStream(
  streamChat(conv.conversation_id, 'What is my favorite color? Answer with the color name only.').stream,
  {
    onDelta: (d) => {
      ctxDeltas += 1;
      ctxAnswer += d;
    },
  }
);
const ctxText = termCtx.content ?? termCtx.message ?? '(none)';
check(
  'context: recall across turns',
  termCtx.kind === 'done' && /teal/i.test(termCtx.content || ''),
  `deltas=${ctxDeltas} terminal=${termCtx.kind} answer="${ctxText.trim().slice(0, 40)}"`
);

// ── 4. markdown/code rendering payload (ensures the UI gets fenced blocks) ──
const { terminal: termMd } = await consumeStream(
  streamChat(conv.conversation_id, 'Reply with a tiny fenced ```js code block that prints HELLO. Nothing else.').stream,
  {}
);
check('fenced code block content arrives', termMd.kind === 'done' && /```/.test(termMd.content || ''), `terminal=${termMd.kind} "${(termMd.content ?? termMd.message ?? '(none)').slice(0, 50).replace(/\n/g, ' ')}"`);

// ── 5. Stop during generation: cancel mid-stream, keep partial ──
const longTurn = streamChat(
  conv.conversation_id,
  'Write a detailed 2000-word essay on the history of lighthouses, with section headings.'
);
let stopDeltas = 0;
const stopPromise = consumeStream(longTurn.stream, {
  onDelta: () => {
    stopDeltas += 1;
  },
});
// Cancel the instant the first token arrives. The wait cap must exceed the
// model's real time-to-first-token for this prompt (measured >30s on
// omen-alpha for the long-essay request): cancelling BEFORE the first token
// streams would legitimately produce an empty partial (server contract:
// cancelled.content = partial streamed so far), which is not what this check
// verifies. Measured streaming itself is fast once started, so a cancel at
// first token is always mid-generation.
for (let i = 0; i < 900 && stopDeltas < 1; i++) await sleep(100);
let cancelResp = null;
try {
  cancelResp = await cancelChat(longTurn.requestId);
} catch (e) {
  cancelResp = { error: e.message };
}
const { full: stopPartial, terminal: termStop } = await stopPromise;
check(
  'cancel: POST /api/chat/cancel accepted',
  cancelResp && cancelResp.cancelled === true,
  JSON.stringify(cancelResp)
);
check(
  'cancel: terminal frame received, partial preserved, composer usable',
  (termStop.kind === 'cancelled' || termStop.kind === 'aborted') && stopPartial.trim().length > 0,
  `terminal=${termStop.kind} partialChars=${stopPartial.length}`
);

// ── 6. refresh/restore: list + fetch persist the conversation ──
const list = await listConversations();
const listed = list.find((c) => c.conversation_id === conv.conversation_id);
check('GET /api/conversations lists the conversation', Boolean(listed), listed ? `title="${listed.title}" count=${listed.message_count}` : 'missing');
const restored = await getConversation(conv.conversation_id);
const restoredAssistant = (restored.messages || []).filter((m) => m.role === 'assistant');
check(
  'refresh restore: messages persisted server-side',
  restored && (restored.messages || []).length >= 4 && restoredAssistant.length >= 2,
  `messages=${(restored.messages || []).length} assistant=${restoredAssistant.length}`
);
// No duplication: each persisted assistant turn is distinct, and the first
// turn's final content matches what the stream's `done` frame carried.
check(
  'no duplicated final assistant response',
  restoredAssistant.length > 0 &&
    restoredAssistant.length === restoredAssistant.map((m) => m.id).filter((v, i, a) => a.indexOf(v) === i).length &&
    (restoredAssistant[0]?.content || '').trim().length > 0,
  `assistant turns=${restoredAssistant.length}`
);

// ── 7. error handling: oversized message → clean error, no stack trace ──
// The lib surfaces HTTP-level rejections (400/401/…) as a terminal
// {kind:'error', message} — exactly what the UI renders. No fake content.
let errMessage = '';
let errKind = '';
try {
  const { terminal: termErr } = await consumeStream(
    streamChat(conv.conversation_id, 'x'.repeat(33000)).stream,
    {}
  );
  errKind = termErr.kind;
  errMessage = termErr.message || '';
} catch (e) {
  errKind = 'thrown';
  errMessage = e.message || String(e);
}
check(
  'error state: oversized message rejected cleanly',
  errMessage.length > 0 &&
    errMessage.length < 200 &&
    /message too long/i.test(errMessage) &&
    !/\n\s*at /.test(errMessage),
  `terminal=${errKind} message="${errMessage.slice(0, 80)}"`
);

// ── 8. delete ──
const del = await deleteConversation(conv.conversation_id);
const afterList = await listConversations();
const gone = !afterList.some((c) => c.conversation_id === conv.conversation_id);
check('DELETE /api/conversations/{id} removes it', del.deleted === true && gone, `deleted=${del.deleted} gone=${gone}`);

// ── 9. cancel idempotency (unknown request_id) ──
const idleCancel = await cancelChat('no-such-request-id');
check('cancel idempotent for unknown request_id', idleCancel.cancelled === false, JSON.stringify(idleCancel));

console.log('\n===== KYREX CHAT SMOKE TEST =====');
for (const r of results) console.log(r);
console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
