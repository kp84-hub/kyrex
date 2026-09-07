/**
 * Engine failure-path transitions — send failures and engine termination.
 *
 * These are the pure state transitions behind App.tsx's send/closed
 * handlers, kept dependency-free (plain erasable TypeScript) so Node can
 * verify them directly (node --experimental-strip-types).
 *
 * The chat_done contract itself is untouched — this module only handles
 * what happens when the engine does NOT respond: a rejected send, or the
 * engine process terminating.
 */
import { applyChatDone, type ChatLine } from "./chatProtocol.ts";

export interface EngineStreamState {
  lines: ChatLine[];
  streaming: boolean;
  buffer: string;
}

/**
 * Transition for a rejected sendToEngine() call.
 *
 * - Seals any in-flight streaming bubble, preserving whatever streamed
 *   (the partial is committed as-is; it is never erased or duplicated).
 * - Resets the streaming flags so later tokens cannot target the wrong line.
 * - Appends a VISIBLE system error line — a failed send must never vanish
 *   silently.
 *
 * The caller flips engineReady to false: a live engine accepts writes.
 */
export function onSendFailure(
  state: EngineStreamState,
  error: unknown
): EngineStreamState {
  const errText = error instanceof Error ? error.message : String(error);
  let { lines, streaming, buffer } = state;
  if (streaming) {
    lines = applyChatDone(lines, buffer, true);
    streaming = false;
    buffer = "";
  }
  return {
    lines: [...lines, { role: "system", content: `[send failed] ${errText}` }],
    streaming,
    buffer,
  };
}

/**
 * Transition for bridge-closed / engine termination.
 *
 * - Seals any in-flight partial assistant content (preserving it as a
 *   finished bubble) BEFORE appending the closure notice, so the notice is
 *   never inserted into the streaming bubble.
 * - Resets streaming flags — no stale streaming state survives the engine.
 * - Appends the "[engine closed]" indication.
 *
 * The caller flips engineReady to false.
 */
export function onEngineClosed(state: EngineStreamState): EngineStreamState {
  let { lines, streaming, buffer } = state;
  if (streaming) {
    lines = applyChatDone(lines, buffer, true);
    streaming = false;
    buffer = "";
  }
  return {
    lines: [...lines, { role: "system", content: "[engine closed]" }],
    streaming: false,
    buffer: "",
  };
}