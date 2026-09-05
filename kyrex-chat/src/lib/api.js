// api.js — Kyrex Chat backend client.

const BASE = '/api';

async function handle(resp) {
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      if (body && body.detail) detail = body.detail;
    } catch {
      /* not json */
    }
    const err = new Error(detail || `Request failed (${resp.status})`);
    err.status = resp.status; // 401 → the UI offers the same-origin sign-in link
    throw err;
  }
  return resp.json();
}

export async function listConversations() {
  const resp = await fetch(`${BASE}/conversations`);
  const data = await handle(resp);
  return data.conversations || [];
}

export async function createConversation() {
  return handle(
    await fetch(`${BASE}/conversations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    })
  );
}

export async function getConversation(conversationId) {
  return handle(await fetch(`${BASE}/conversations/${conversationId}`));
}

export async function deleteConversation(conversationId) {
  return handle(
    await fetch(`${BASE}/conversations/${conversationId}`, { method: 'DELETE' })
  );
}

export async function chatStatus() {
  return handle(await fetch(`${BASE}/chat/status`));
}

// Generates a client-side request_id so /api/chat/cancel can target the
// exact in-flight generation (the backend keys its cancel registry on it).
export function newRequestId() {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID();
  return `req-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

// Cancels an in-flight generation server-side (idempotent when unknown).
export async function cancelChat(requestId) {
  return handle(
    await fetch(`${BASE}/chat/cancel`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_id: requestId }),
    })
  );
}

// Opens an SSE stream for a chat turn. Returns an object with `cancel()`,
// an async iterator-compatible `stream` of parsed events, and the
// `requestId` that was sent so Stop can cancel this exact stream.
export function streamChat(conversationId, message, requestId) {
  const controller = new AbortController();
  const reqId = requestId || newRequestId();

  async function* stream() {
    const resp = await fetch(`${BASE}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        conversation_id: conversationId || '',
        message,
        request_id: reqId,
      }),
      signal: controller.signal,
    });

    if (!resp.ok) {
      let detail = resp.statusText;
      try {
        const body = await resp.json();
        if (body && body.detail) detail = body.detail;
      } catch {
        /* not json */
      }
      const err = new Error(detail || `Request failed (${resp.status})`);
      err.status = resp.status; // 401 → the UI offers the same-origin sign-in link
      throw err;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line.
      let idx;
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        if (!frame.trim()) continue;
        for (const line of frame.split('\n')) {
          if (line.startsWith('data:')) {
            const payload = line.slice(5).trim();
            if (payload) yield JSON.parse(payload);
          }
        }
      }
    }
  }

  // Return the started iterator (not the generator function): consumers do
  // `for await (const event of stream)` on it exactly once.
  return { stream: stream(), cancel: () => controller.abort(), requestId: reqId };
}
