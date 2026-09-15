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

// ── 9. duplicate assistant response (one user message, answer twice) ──
// The exact regression: the engine concatenates every round of a turn and
// joins them with a bare newline for the authoritative chat_done payload
// (kyrex_engine/kyrex/core.py `full_text = "\n".join(collected_content)`),
// while streaming "\n\n---\n" between the rounds. One user message therefore
// reached the transcript as the SAME assistant answer twice.
{
  const ANSWER = "I'm Kyrex. I work directly against your project files.";

  // (a) authoritative done payload: bare-newline joined rounds.
  const doneShape = `${ANSWER}\n${ANSWER}`;
  assert.equal(sanitizeAssistantText(doneShape), ANSWER,
    "bare-newline repeated answer must collapse to one copy");

  // (b) the streamed delta shape (divider emitted between rounds).
  const deltaShape = `${ANSWER}\n\n---\n${ANSWER}`;
  assert.equal(sanitizeAssistantText(deltaShape), ANSWER,
    "streamed repeated answer must collapse to one copy");

  // (c) three rounds, and multi-line answers, collapse the same way.
  assert.equal(sanitizeAssistantText(`${ANSWER}\n${ANSWER}\n${ANSWER}`), ANSWER);
  assert.equal(
    sanitizeAssistantText("line one\nline two\nline one\nline two"),
    "line one\nline two");

  // (d) one user turn -> exactly ONE assistant bubble carrying ONE copy,
  // whether the terminal frame is applied once or the message is re-finalized.
  const messages = simulateTurn(
    "what is your role?",
    [`${ANSWER}\n\n---\n`, ANSWER],
    doneShape
  );
  const assistants = messages.filter((m) => m.role === "assistant");
  assert.equal(assistants.length, 1, "one user message -> one assistant message");
  assert.equal(messages.filter((m) => m.role === "user").length, 1);
  assert.equal(assistants[0].content, ANSWER, "the answer is not appended twice");
  assert.equal(assistants[0].content.split("I'm Kyrex").length - 1, 1);

  // (e) replay/re-finalization is idempotent (the persisted content is the
  // same single copy as the final event, so a reconnect cannot duplicate it).
  const replayed = sanitizeAssistantText(sanitizeAssistantText(doneShape));
  assert.equal(replayed, ANSWER, "finalization must be idempotent");
  const reloaded = sanitizeConversation({
    conversation_id: "c1",
    messages: [{ id: "a", role: "assistant", content: doneShape }],
  });
  assert.equal(reloaded.messages[0].content, ANSWER,
    "replayed transcript renders one copy");

  // (f) distinct content is never merged, and error text stays intact.
  assert.equal(sanitizeAssistantText("First part.\nSecond part."),
    "First part.\nSecond part.");
  const err = "[OpenAI Provider Error: upstream 500 — request failed]";
  assert.equal(sanitizeAssistantText(err).trim(), err.trim());
}

console.log("✓ chat sanitizer: internal markers, round collapse, dedupe, duplicate-response, errors, one-bubble contract verified.");
