// sanitize.js — user-facing assistant text.
//
// The Kyrex engine appends INTERNAL lifecycle markers to its final
// `chat_done` content (mirrored by tui/update_engine.go's
// extractTaskCompleteSummary):
//
//   [Task Complete: …]                     — the model's completion summary
//   [continue] …                           — the tool-less-round nudge
//   [!] Task not verified complete — …     — loop detector / circuit breaker
//   [!] Max recursion depth reached.
//
// That content is authoritative and is what the backend streams (SSE `done`)
// and persists, so without this boundary the markers became assistant message
// text. This module is the CLIENT half of the presentation boundary — the
// backend (chat_service.sanitize_assistant_text) is the source of truth; this
// mirror guarantees nothing internal ever renders even if a payload slips
// through. Task-completion SEMANTICS are unchanged: only rendering changes.
//
// Deliberately NOT stripped: real error text. A failure must stay visible.

const MARKER_LINE_PATTERNS = [
  /^\s*\[Task Complete(?::[^\]]*)?\]\s*$/,
  /^\s*\[Task assumed complete[^\]]*\]\s*$/,
  /^\s*\[continue\][^\n]*$/,
  /^\s*\[!\]\s*Task not verified complete[^\n]*$/,
  /^\s*\[!\]\s*Max recursion depth reached\.?\s*$/,
  /^\s*\[Model produced reasoning but no display content\.[^\]]*\]\s*$/,
];

// The engine streams this divider between provider rounds of one turn
// (kyrex_engine/kyrex/core.py `streamer("\n\n---\n")`). Collapsing it keeps
// multiple internal rounds reading as one coherent response.
const ROUND_DIVIDER_RE = /\n\s*\n\s*---\s*\n\s*\n/g;

export function isInternalMarkerLine(line) {
  return MARKER_LINE_PATTERNS.some((re) => re.test(line));
}

/**
 * Strip internal control markers from assistant output for display.
 *
 * Removes internal lifecycle marker lines, collapses the engine's inter-round
 * divider so multiple rounds read as one coherent response, and drops a
 * paragraph that merely repeats the one above it (the engine concatenates
 * every round's content). Real error text is never removed.
 */
export function sanitizeAssistantText(text) {
  if (text == null) return "";
  let cleaned = String(text).replace(/\r\n/g, "\n");
  cleaned = cleaned.replace(ROUND_DIVIDER_RE, "\n\n");
  const kept = cleaned
    .split("\n")
    .filter((line) => !isInternalMarkerLine(line));
  cleaned = kept.join("\n");
  const blocks = cleaned.split(/\n\s*\n/);
  const deduped = [];
  let prevKey = null;
  for (const block of blocks) {
    const key = block.trim();
    if (key && key === prevKey) continue;
    deduped.push(block);
    prevKey = key;
  }
  return deduped.join("\n\n").trim();
}

/**
 * Sanitize the assistant messages of a persisted conversation for rendering.
 * Returns a shallow copy; the stored record is never mutated.
 */
export function sanitizeConversation(conv) {
  if (!conv || !Array.isArray(conv.messages)) return conv;
  return {
    ...conv,
    messages: conv.messages.map((m) =>
      m && m.role === "assistant" && typeof m.content === "string"
        ? { ...m, content: sanitizeAssistantText(m.content) }
        : m
    ),
  };
}
