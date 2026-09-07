/**
 * Engine failure-path verification — send failures, engine termination,
 * and readiness.
 *
 * Drives the same logic App.tsx now uses (sendToEngineChecked via
 * onSendFailure; bootEngine's onClosed handler via onEngineClosed) and
 * asserts the FINAL visible conversation state plus streaming/readiness
 * flags — mirroring the component handlers exactly, without React.
 *
 * Run:  node --experimental-strip-types tests/engine_failure_contract.test.mjs
 */
import assert from "node:assert/strict";
import { onSendFailure, onEngineClosed } from "../src/lib/engineFailure.ts";
import { applyChatDone } from "../src/lib/chatProtocol.ts";

// ── handlers mirroring App.tsx ─────────────────────────────────────────

// Mirrors handleSend() + sendToEngineChecked(): appends the user line,
// then checked-sends; on rejection, applies onSendFailure and flips ready.
async function attemptSend(state, payload, sendFn) {
  const st = { ...state, lines: [...state.lines, { role: "user", content: payload.content }] };
  try {
    await sendFn(payload);
    return st; // success path touches nothing else
  } catch (e) {
    const next = onSendFailure({ lines: st.lines, streaming: st.streaming, buffer: st.buffer }, e);
    return { lines: next.lines, streaming: next.streaming, buffer: next.buffer, ready: false };
  }
}

// Mirrors bootEngine's onClosed callback: seal + reset flags + flip ready.
function onClosed(state) {
  const next = onEngineClosed({ lines: state.lines, streaming: state.streaming, buffer: state.buffer });
  return { lines: next.lines, streaming: next.streaming, buffer: next.buffer, ready: false };
}

// Mirrors handleMessage's token handling: streaming into the last agent
// line when streaming, else opening a new bubble.
function applyToken(state, content) {
  const buffer = (state.buffer ?? "") + (content ?? "");
  if (state.streaming) {
    return { ...state, buffer, lines: [...state.lines.slice(0, -1), { role: "agent", content: buffer }] };
  }
  return { ...state, streaming: true, buffer, lines: [...state.lines, { role: "agent", content: buffer }] };
}

// Mirrors handleSend's guard: sends are blocked when the engine is not ready.
function guardedSend(state, text, sendFn) {
  if (!text.trim() || !state.ready) return state;
  return attemptSend(state, { type: "chat", content: text }, sendFn);
}

const READY = { lines: [], streaming: false, buffer: "", ready: true };

// 1. successful send: unchanged behavior, nothing appended, ready intact.
{
  const t = await attemptSend(
    READY,
    { type: "chat", content: "hello" },
    async () => {} // resolves
  );
  assert.deepEqual(t.lines, [{ role: "user", content: "hello" }]);
  assert.equal(t.streaming, false, "no streaming state created");
  assert.equal(t.buffer, "");
  assert.equal(t.ready, true, "successful send keeps engine healthy");
}

// 2. rejected sendToEngine() → visible error, ready flipped false.
{
  const t = await attemptSend(
    READY,
    { type: "chat", content: "hello" },
    async () => {
      throw new Error("engine not started");
    }
  );
  assert.deepEqual(
    t.lines.map((l) => [l.role, l.content]),
    [
      ["user", "hello"],
      ["system", "[send failed] engine not started"],
    ],
    "failure is visible in the conversation, never silent"
  );
  assert.equal(t.ready, false, "failed send must not report a healthy engine");
}

// 3. engine termination → engineReady === false.
{
  const t = onClosed(READY);
  assert.equal(t.ready, false);
  assert.deepEqual(t.lines, [{ role: "system", content: "[engine closed]" }]);
  assert.equal(t.streaming, false);
  assert.equal(t.buffer, "");
}

// 4. engine termination during streaming → streaming state reset,
//    partial content preserved as a sealed bubble, notice after it.
{
  const streamed = applyToken(READY, "Partial answer");
  assert.equal(streamed.streaming, true);
  const t = onClosed(streamed);
  assert.equal(t.ready, false);
  assert.equal(t.streaming, false, "streaming flag reset");
  assert.equal(t.buffer, "", "buffer cleared");
  assert.deepEqual(
    t.lines.map((l) => [l.role, l.content]),
    [
      ["agent", "Partial answer"],
      ["system", "[engine closed]"],
    ],
    "partial preserved and closure notice appended after the bubble"
  );
}

// 5. tokens → engine termination: partial preserved, no stale streaming
//    state, and a later stale token cannot target the wrong line.
{
  const mid = applyToken(applyToken(READY, "This is a "), "partial");
  const t = onClosed(mid);
  assert.equal(t.streaming, false);
  assert.equal(t.buffer, "");
  assert.deepEqual(t.lines.map((l) => l.content), ["This is a partial", "[engine closed]"]);

  // Stale token after close: with streaming reset, it opens a NEW bubble
  // instead of clobbering the [engine closed] system line.
  const stale = applyToken(t, "x");
  assert.equal(stale.lines.length, 3);
  assert.deepEqual(stale.lines[1], { role: "system", content: "[engine closed]" });
  assert.deepEqual(stale.lines[2], { role: "agent", content: "x" });
}

// 6a. later send after engine termination: blocked by the readiness guard —
//     nothing vanishes silently because nothing is sent.
{
  const closed = onClosed(applyToken(READY, "previous partial"));
  const before = closed.lines.length;
  const t = guardedSend(closed, "hello again", async () => {
    throw new Error("must not be called");
  });
  assert.equal(t.ready, false);
  assert.equal(t.lines.length, before, "guard prevents the send entirely");
}

// 6b. a send that slips through (stale ready snapshot) after the engine is
//     gone is rejected by Rust and surfaced cleanly instead of disappearing.
{
  const closed = onClosed(READY);
  const staleReady = { ...closed, ready: true }; // racy closure before close lands
  const t = await attemptSend(
    staleReady,
    { type: "chat", content: "hello" },
    async () => {
      throw new Error("failed to write to engine stdin: Broken pipe");
    }
  );
  assert.equal(t.ready, false, "slip-through send flips readiness off");
  assert.deepEqual(
    t.lines.map((l) => [l.role, l.content]),
    [
      ["system", "[engine closed]"],
      ["user", "hello"],
      ["system", "[send failed] failed to write to engine stdin: Broken pipe"],
    ],
    "rejection is visible; nothing silently disappears"
  );
}

// Send failure while streaming (previous turn interrupted by engine death):
// the in-flight bubble is sealed and the failure is surfaced.
{
  const mid = applyToken(READY, "old partial stream");
  const t = await attemptSend(
    mid,
    { type: "chat", content: "new message" },
    async () => {
      throw new Error("engine not started");
    }
  );
  assert.equal(t.streaming, false, "streaming state restored after failed send");
  assert.equal(t.buffer, "");
  assert.equal(t.ready, false);
  assert.deepEqual(
    t.lines.map((l) => [l.role, l.content]),
    [
      ["agent", "old partial stream"],
      ["user", "new message"],
      ["system", "[send failed] engine not started"],
    ],
    "partial preserved, failure visible"
  );
}

// onSendFailure / onEngineClosed leave the chat_done contract untouched.
{
  const lines = applyChatDone(
    [{ role: "agent", content: "streamed" }],
    "authoritative",
    true
  );
  assert.deepEqual(lines, [{ role: "agent", content: "authoritative" }]);
}

console.log("✓ engine failure path: send failures surfaced, termination resets state, readiness honest.");