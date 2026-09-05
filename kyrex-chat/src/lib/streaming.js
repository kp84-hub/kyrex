// streaming.js — SSE consumption for Kyrex Chat.
//
// Consumes the Phase 2 SSE contract exactly as the backend emits it:
//   * conversation — {type, conversation_id}   (once, first frame)
//   * delta        — {type, content}           (0..N incremental tokens)
//   * done         — {type, content, conversation_id}  (terminal, success)
//   * error        — {type, message}           (terminal, provider/engine failure)
//   * cancelled    — {type, content}           (terminal, user/client cancelled)
//
// Exactly one terminal frame arrives per request. The resolved value carries
// the authoritative terminal outcome so callers never have to guess, and the
// final assistant text is never duplicated: `done.content` is the server's
// authoritative full text and replaces the accumulated deltas.

function readableError(err) {
  // Human-readable, no stack traces.
  if (err instanceof Error) return err.message || 'Request failed';
  return String(err || 'Request failed');
}

// Consumes an SSE stream (async iterator of parsed events) and drives the
// provided callbacks. Resolves with { full, terminal } where terminal is one
// of: {kind:'done', content, conversationId} | {kind:'cancelled', content} |
// {kind:'error', message} | {kind:'aborted', content} (local transport abort).
export async function consumeStream(stream, handlers = {}) {
  let full = '';
  let terminal = null;

  try {
    for await (const event of stream) {
      const t = event && event.type;
      if (t === 'conversation') {
        handlers.onConversation?.(event.conversation_id);
      } else if (t === 'delta' && typeof event.content === 'string') {
        full += event.content;
        handlers.onDelta?.(event.content);
      } else if (t === 'done') {
        terminal = {
          kind: 'done',
          // The server's terminal content is authoritative (it is the exact
          // text that was persisted) and must not be duplicated client-side.
          content: typeof event.content === 'string' ? event.content : full,
          conversationId: event.conversation_id,
        };
        break;
      } else if (t === 'cancelled') {
        terminal = {
          kind: 'cancelled',
          content: typeof event.content === 'string' ? event.content : full,
        };
        break;
      } else if (t === 'error') {
        terminal = { kind: 'error', message: event.message || 'Stream error' };
        break;
      }
      // Unknown event types are ignored (forward compatibility).
    }
  } catch (err) {
    if (err && err.name === 'AbortError') {
      // Local transport abort (browser-side cancel fallback): keep partial.
      terminal = { kind: 'aborted', content: full };
    } else {
      terminal = { kind: 'error', message: readableError(err) };
    }
  }

  if (!terminal) terminal = { kind: 'aborted', content: full };
  return { full, terminal };
}
