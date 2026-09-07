/**
 * chat_done contract — converges with the sibling clients:
 *
 *   PRIMARY:  Go TUI  (tui/update_engine.go -> handleChatDone)
 *             finalRes := msg.Content; if finalRes == "" { finalRes = m.CurrToken }
 *   SECONDARY: Python chat backend (kyrex-cloud/web/backend/chat_service.py)
 *             authoritative = engine chat_done content; replaces accumulated deltas.
 *
 * `chat_done.content` is AUTHORITATIVE when non-empty. When it is empty
 * (interrupt, tool-only turn, provider error before tokens) the accumulated
 * streamed tokens are preserved. Final content REPLACES the streaming bubble;
 * it is never appended to the streamed deltas (that duplicates the response),
 * and an empty `chat_done.content` never erases streamed content.
 *
 * This module is deliberately plain erasable TypeScript so Node can run it
 * directly (node --experimental-strip-types) for protocol verification.
 */
export interface ChatLine {
  role: "user" | "agent" | "system";
  content: string;
}

/**
 * Resolve the final assistant content for a chat_done frame.
 *
 *   const finalContent = msg.content?.trim() ? msg.content : accumulatedStreamingContent;
 *
 * Non-empty done content wins (authoritative final result); empty done
 * content falls back to the accumulated streamed tokens.
 */
export function resolveFinalContent(
  doneContent: string | undefined | null,
  streamed: string
): string {
  return doneContent && doneContent.trim() ? doneContent : streamed;
}

/**
 * Apply chat_done to the visible conversation: REPLACE the in-flight
 * assistant bubble (the most recent agent line) with finalContent.
 *
 * - Streaming bubble exists  -> last agent line is replaced (never appended).
 * - No bubble, content       -> append a new agent line (turn completed
 *   without visible tokens, e.g. tool-only synthesis).
 * - No bubble, no content    -> conversation untouched: a provider error
 *   before any tokens leaves no assistant bubble, while the separate error
 *   frame stays visible.
 *
 * The last AGENT line (not the last line) is targeted so an error/system
 * frame appended after the stream (provider error after streaming) is never
 * clobbered by finalization.
 */
export function applyChatDone(
  lines: ChatLine[],
  finalContent: string,
  isStreaming: boolean
): ChatLine[] {
  let lastAgent = -1;
  for (let i = lines.length - 1; i >= 0; i--) {
    if (lines[i].role === "agent") {
      lastAgent = i;
      break;
    }
  }
  if (lastAgent !== -1 && (isStreaming || finalContent)) {
    const copy = [...lines];
    copy[lastAgent] = { role: "agent", content: finalContent };
    return copy;
  }
  if (finalContent) {
    return [...lines, { role: "agent", content: finalContent }];
  }
  return lines;
}