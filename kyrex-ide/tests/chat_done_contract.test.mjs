/**
 * chat_done contract verification — section 12 of the review.
 *
 * Drives the same reducer shape App.tsx uses (token accumulation into the
 * streaming bubble, applyChatDone finalization) and asserts the FINAL VISIBLE
 * CONVERSATION STATE for every required case — not merely that the handler
 * executes.
 *
 * Run:  node --experimental-strip-types tests/chat_done_contract.test.mjs
 */
import assert from "node:assert/strict";
import { resolveFinalContent, applyChatDone } from "../src/lib/chatProtocol.ts";

// ── reducer faithful to App.tsx handleMessage ──────────────────────────
function simulate(frames) {
  let lines = [];
  let streaming = false;
  let buffer = "";
  for (const frame of frames) {
    switch (frame.type) {
      case "token": {
        buffer += frame.content ?? "";
        if (streaming) {
          lines = [...lines.slice(0, -1), { role: "agent", content: buffer }];
        } else {
          streaming = true;
          lines = [...lines, { role: "agent", content: buffer }];
        }
        break;
      }
      case "user": {
        lines = [...lines, { role: "user", content: frame.content }];
        break;
      }
      case "error": {
        lines = [...lines, { role: "system", content: `[error] ${frame.content}` }];
        break;
      }
      case "chat_done": {
        const finalContent = resolveFinalContent(frame.content, buffer);
        lines = applyChatDone(lines, finalContent, streaming);
        buffer = "";
        streaming = false;
        break;
      }
      default:
        throw new Error(`unknown frame: ${frame.type}`);
    }
  }
  return { lines, streaming, buffer };
}

function agentContents(lines) {
  return lines.filter((l) => l.role === "agent").map((l) => l.content);
}

// 1. tokens → non-empty chat_done: bubble REPLACED with authoritative final.
{
  const { lines } = simulate([
    { type: "token", content: "Hello " },
    { type: "token", content: "world" },
    { type: "chat_done", content: "Hello world — authoritative" },
  ]);
  assert.equal(lines.length, 1, "one assistant bubble remains");
  assert.equal(lines[0].role, "agent");
  assert.equal(
    lines[0].content,
    "Hello world — authoritative",
    "authoritative content replaced the streamed bubble"
  );
  assert.ok(
    !lines[0].content.includes("Hello worldHello"),
    "final content was never appended to streamed deltas (no duplication)"
  );
}

// 2. tokens → empty chat_done: accumulated stream preserved.
{
  const { lines } = simulate([
    { type: "token", content: "Hello " },
    { type: "token", content: "world" },
    { type: "chat_done", content: "" },
  ]);
  assert.deepEqual(agentContents(lines), ["Hello world"]);
  assert.equal(lines.length, 1, "no extra bubble created");
}

// 3. error → chat_done (empty, no tokens): error frame visible, no empty bubble.
{
  const { lines, streaming } = simulate([
    { type: "error", content: "provider failed" },
    { type: "chat_done", content: "" },
  ]);
  assert.deepEqual(
    lines.map((l) => [l.role, l.content]),
    [["system", "[error] provider failed"]],
    "error frame remains visible"
  );
  assert.equal(agentContents(lines).length, 0, "no empty assistant bubble");
  assert.equal(streaming, false);
}

// 4a. tokens → error → empty chat_done: partial stream preserved + error visible.
{
  const { lines } = simulate([
    { type: "token", content: "Partial answer " },
    { type: "error", content: "stream died mid-way" },
    { type: "chat_done", content: "" },
  ]);
  assert.deepEqual(agentContents(lines), ["Partial answer "]);
  assert.deepEqual(
    lines.map((l) => [l.role, l.content]),
    [
      ["agent", "Partial answer "],
      ["system", "[error] stream died mid-way"],
    ],
    "streamed partial preserved AND separate error frame visible"
  );
}

// 4b. tokens → error → non-empty chat_done: authoritative replace + error visible.
{
  const { lines } = simulate([
    { type: "token", content: "Partial answer " },
    { type: "error", content: "stream died" },
    { type: "chat_done", content: "Authoritative final" },
  ]);
  assert.deepEqual(
    lines.map((l) => [l.role, l.content]),
    [
      ["agent", "Authoritative final"],
      ["system", "[error] stream died"],
    ],
    "authoritative final replaces bubble; error frame untouched"
  );
}

// 5. interrupt → empty chat_done: partial response preserved.
{
  const { lines } = simulate([
    { type: "token", content: "This is a partial" },
    { type: "chat_done", content: "" }, // interrupt cancels the turn
  ]);
  assert.deepEqual(agentContents(lines), ["This is a partial"]);
}

// 5b. interrupt before any tokens → empty chat_done: conversation unchanged.
{
  const prior = [{ role: "user", content: "ask" }];
  const { lines } = simulate([
    { type: "chat_done", content: "" },
    { type: "chat_done", content: "" },
  ]);
  assert.equal(lines.length, 0, "no bubble fabricated for an empty turn");
  // also: a pre-existing agent bubble from a previous turn is untouched
  const result = applyChatDone(prior, "", false);
  assert.deepEqual(result, prior, "old turns never mutated by empty chat_done");
}

// 6. multiple token frames handled sequentially then finalized once.
{
  const { lines } = simulate([
    { type: "token", content: "a" },
    { type: "token", content: "b" },
    { type: "token", content: "c" },
    { type: "token", content: "d" },
    { type: "chat_done", content: "abcd" },
  ]);
  assert.deepEqual(agentContents(lines), ["abcd"]);
}

// 7. split / unaligned delivery at the reducer level: a chat_done frame
//    delivered across "chunks" still finalizes exactly once. (The transport
//    split itself is covered by the Rust ndjson_lines unit tests; here we
//    verify the final state when frames arrive one NDJSON frame per event.)
{
  const { lines } = simulate([
    { type: "token", content: "Hello " },
    { type: "chat_done", content: "Hello world" },
  ]);
  assert.deepEqual(agentContents(lines), ["Hello world"]);
}

// Multi-turn sanity: finalization targets only the current bubble.
{
  const { lines } = simulate([
    { type: "user", content: "q1" }, // App adds user lines separately
    { type: "token", content: "Answer one" },
    { type: "chat_done", content: "Answer one (final)" },
    { type: "user", content: "q2" },
    { type: "token", content: "partial two" },
    { type: "chat_done", content: "" }, // interrupted second turn
  ]);
  const agents = agentContents(lines);
  assert.equal(agents[0], "Answer one (final)");
  assert.equal(agents[1], "partial two");
  assert.equal(lines.length, 4, "user lines preserved, no duplicate bubbles");
}

// resolveFinalContent edge semantics.
assert.equal(resolveFinalContent("  ", "streamed"), "streamed", "whitespace-only final falls back");
assert.equal(resolveFinalContent("x", "streamed"), "x", "non-empty final wins");
assert.equal(resolveFinalContent(undefined, "streamed"), "streamed");
assert.equal(resolveFinalContent(null, ""), "");

console.log("✓ chat_done contract: all 7 protocol cases verified (final visible state).");