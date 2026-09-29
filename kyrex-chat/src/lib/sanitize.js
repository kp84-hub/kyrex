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

// The engine surrounds this divider with the content of the rounds it
// separates: it streams `"\n\n---\n"` between provider rounds of one turn
// (kyrex_engine/kyrex/core.py `streamer("\n\n---\n")`), so the next round's
// content follows the dashes immediately — there is no blank line after them.
// Both that shape and a blank-line-padded `---` collapse here.
const ROUND_DIVIDER_RE = /\n[ \t]*\n[ \t]*---[ \t]*\n[ \t]*/g;

export function isInternalMarkerLine(line) {
  return MARKER_LINE_PATTERNS.some((re) => re.test(line));
}

/**
 * Drop answer blocks that repeat immediately at the head of `text`.
 *
 * The engine concatenates the content of every round of one turn and joins
 * them with a bare newline for the authoritative `chat_done` payload
 * (`full_text = "\n".join(collected_content)`), so a turn whose rounds each
 * produced the same answer arrives as `answer + "\n" + answer` and would
 * render as the same reply twice. Collapsing that repeated leading run is what
 * keeps one user turn to one assistant answer, however many rounds produced
 * it. Only a byte-identical run of leading lines is dropped, so distinct
 * content is never merged and the message keeps its own identity/id.
 */
export function collapseRepeatedAnswer(text) {
  let lines = String(text == null ? "" : text).split("\n");
  let changed = true;
  while (changed) {
    changed = false;
    const total = lines.length;
    for (let size = Math.floor(total / 2); size > 0; size--) {
      const head = lines.slice(0, size);
      const next = lines.slice(size, size * 2);
      if (head.length === next.length && head.every((l, i) => l === next[i])) {
        lines = lines.slice(size);
        changed = true;
        break;
      }
    }
  }
  return lines.join("\n");
}

/**
 * Strip internal control markers from assistant output for display.
 *
 * Removes internal lifecycle marker lines, collapses the engine's inter-round
 * divider so multiple rounds read as one coherent response, and drops a
 * paragraph — or an answer joined by the engine's bare-newline round
 * separator — that merely repeats the one above it (the engine concatenates
 * every round's content). Idempotent: running it twice never changes the
 * result, so a replayed/re-finalized message renders exactly one copy. Real
 * error text is never removed.
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
  return collapseRepeatedAnswer(deduped.join("\n\n").trim());
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
