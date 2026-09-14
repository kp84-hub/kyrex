// sanitize.test.mjs — the client half of the Chat presentation boundary.
//
// Proves that internal engine control markers ([Task Complete: …],
// [continue] …, loop-detector / circuit-breaker diagnostics,
// "[!] Max recursion depth reached.") never render as assistant text, that
// multiple engine rounds collapse into one coherent response, that real errors
// stay visible, and that the bubble reducer keeps exactly one assistant
// message per user turn.
//
// Run: node tests/sanitize.test.mjs
import assert from "node:assert/strict";
import {
  sanitizeAssistantText,
  sanitizeConversation,
  isInternalMarkerLine,
} from "../src/lib/sanitize.js";

const MARKERS = [
  "[Task Complete: Added the changelog entry.]",
  "[continue] Two consecutive tool-less rounds and task_complete was not called.",
  "[!] Task not verified complete — loop detected: repeating identical tool calls 3+ times. Aborting reasoning loop.",
  "[!] Task not verified complete — circuit breaker: 3 consecutive tool failures. Aborting.",
  "[!] Max recursion depth reached.",
];

// ── 1. every internal marker is stripped ───────────────────────────
{
  const raw =
    "I updated the file and the tests pass.\n" +
    MARKERS.join("\n") +
    "\nEverything is green.";
  const out = sanitizeAssistantText(raw);
  for (const m of MARKERS) {
    assert.ok(!out.includes(m), `marker leaked: ${m}`);
  }
  assert.ok(out.includes("I updated the file"));
  assert.ok(out.includes("Everything is green."));
  assert.ok(MARKERS.every(isInternalMarkerLine));
}

// ── 2. rounds collapse + adjacent duplicates dedupe ────────────────
{
  const raw = "Hey there\n\n---\n\nHey there\n\nWhat can I help with?";
  const out = sanitizeAssistantText(raw);
  assert.ok(!out.includes("---"), "inter-round divider must be dropped");
  assert.equal(out.split("Hey there").length - 1, 1, "duplicate round collapsed");
  assert.ok(out.includes("What can I help with?"));
}

// ── 3. only the marker LINE is removed, surrounding prose survives ─
{
  const raw = "Line one\n[continue] nudge text here\nLine two";
  const out = sanitizeAssistantText(raw);
  assert.equal(out, "Line one\nLine two");
}

// ── 4. real errors stay visible (never sanitized away) ─────────────
{
  const err = "[OpenAI Provider Error: upstream 500 — request failed]";
  assert.equal(sanitizeAssistantText(err).trim(), err.trim());
}

// ── 5. recursion-depth + reasoning-only diagnostics ────────────────
{
  const raw = "Answer.\n[!] Max recursion depth reached.\n" +
    "[Model produced reasoning but no display content. Check above output.]";
  const out = sanitizeAssistantText(raw);
  assert.equal(out, "Answer.");
}

// ── 6. sanitizeConversation cleans assistant text, not user text ───
{
  const conv = {
    conversation_id: "c1",
    messages: [
      { id: "u", role: "user", content: "[Task Complete: not stripped here]" },
      { id: "a", role: "assistant", content: "Done.\n" + MARKERS[0] },
    ],
  };
  const out = sanitizeConversation(conv);
  assert.equal(out.messages[0].content, "[Task Complete: not stripped here]");
  assert.ok(!out.messages[1].content.includes(MARKERS[0]));
  // input untouched (no mutation of stored state)
  assert.ok(conv.messages[1].content.includes(MARKERS[0]));
}

// ── 7. one user turn -> one coherent assistant bubble (final state) ─
// Faithful to useChat.js: deltas accumulate into ONE placeholder bubble, and
// the terminal (done) content REPLACES it (sanitized). Multiple engine rounds
// therefore render as a single bubble.
function simulateTurn(userText, deltaChunks, doneContent) {
  let messages = [{ id: "u", role: "user", content: userText }];
  const assistant = { id: "a", role: "assistant", content: "", streaming: true };
  messages = [...messages, assistant];
  for (const chunk of deltaChunks) {
    messages = messages.map((m) =>
      m.id === "a" ? { ...m, content: m.content + chunk } : m
    );
  }
  const finalContent =
    (typeof doneContent === "string" && doneContent.trim())
      ? doneContent
      : messages.find((m) => m.id === "a").content;
  messages = messages.map((m) =>
    m.id === "a"
      ? { ...m, content: sanitizeAssistantText(finalContent), streaming: false }
      : m
  );
  return messages;
}

{
  // Real engine shape: two rounds + markers in the authoritative done content.
  const done =
    "Hey there\n\n---\n\nHey there\n\nHere is the answer.\n" +
    "[continue] nudge\n[Task Complete: Answered.]";
  const messages = simulateTurn(
    "hi",
    ["Hey there\n\n---\n\nHey there\n\n", "Here is the answer.\n"],
    done
  );
  const assistants = messages.filter((m) => m.role === "assistant");
  assert.equal(assistants.length, 1, "exactly one assistant bubble");
  assert.equal(messages.filter((m) => m.role === "user").length, 1);
  const content = assistants[0].content;
  for (const m of MARKERS) assert.ok(!content.includes(m), `leak: ${m}`);
  assert.ok(!content.includes("---"));
  assert.equal(content.split("Hey there").length - 1, 1, "one coherent body");
  assert.ok(content.includes("Here is the answer."));
}

// ── 8. no done content -> streamed partial preserved (no duplication) ─
{
  const messages = simulateTurn("hi", ["partial answer"], "");
  const a = messages.find((m) => m.role === "assistant");
  assert.equal(a.content, "partial answer");
  assert.ok(!a.content.includes("partial answerpartial answer"));
}

console.log("✓ chat sanitizer: internal markers, round collapse, dedupe, errors, one-bubble contract verified.");
