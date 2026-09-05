// useChat.js — the chat state machine.
//
// Consumes the real Phase 2 SSE stream via lib/api.js + lib/streaming.js.
// Terminal-frame contract:
//   done      → assistant message shows the authoritative `done.content`
//               (never duplicated from accumulated deltas)
//   cancelled → partial text is preserved with a "stopped" marker
//   error     → clean, human-readable error (no fake content, no stacks)
// The active conversation id is persisted so a browser refresh restores it.

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  listConversations,
  createConversation,
  getConversation,
  deleteConversation,
  streamChat,
  cancelChat,
  chatStatus,
  newRequestId,
} from '../lib/api';
import { consumeStream } from '../lib/streaming';

const ACTIVE_KEY = 'kyrex-chat.activeConversationId';

function persistActive(id) {
  try {
    if (id) localStorage.setItem(ACTIVE_KEY, id);
    else localStorage.removeItem(ACTIVE_KEY);
  } catch {
    /* storage unavailable — non-fatal */
  }
}

function readActive() {
  try {
    return localStorage.getItem(ACTIVE_KEY) || null;
  } catch {
    return null;
  }
}

export function useChat() {
  const [conversations, setConversations] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [isGenerating, setIsGenerating] = useState(false);
  const [error, setError] = useState(null);
  // True when the backend rejected a call with 401 (no session cookie on this
  // origin). The banner then offers the same-origin GitHub sign-in link — the
  // identical auth entry point the existing Cloud web frontend uses.
  const [needsAuth, setNeedsAuth] = useState(false);
  const [status, setStatus] = useState({ available: true });
  const streamRef = useRef(null); // { cancel, requestId, assistantId }

  const refreshList = useCallback(async () => {
    try {
      const list = await listConversations();
      setConversations(list);
      setNeedsAuth(false);
      return list;
    } catch (e) {
      setError(e.message);
      if (e.status === 401) setNeedsAuth(true);
      return [];
    }
  }, []);

  const refreshStatus = useCallback(async () => {
    try {
      const s = await chatStatus();
      setStatus(s);
    } catch (e) {
      setStatus({ available: false, detail: e.message });
    }
  }, []);

  const loadConversation = useCallback(async (id) => {
    // Switching conversations during generation cancels the in-flight turn
    // so the stream can never append into the wrong conversation view.
    if (streamRef.current) {
      streamRef.current.cancel();
      streamRef.current = null;
      setIsGenerating(false);
    }
    setActiveId(id);
    persistActive(id);
    setMessages([]);
    setError(null);
    try {
      const conv = await getConversation(id);
      setMessages(conv.messages || []);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  const newChat = useCallback(async () => {
    if (streamRef.current) {
      streamRef.current.cancel();
      streamRef.current = null;
      setIsGenerating(false);
    }
    try {
      const conv = await createConversation();
      setActiveId(conv.conversation_id);
      persistActive(conv.conversation_id);
      setMessages([]);
      setError(null);
      await refreshList();
      return conv;
    } catch (e) {
      setError(e.message);
      return null;
    }
  }, [refreshList]);

  const removeConversation = useCallback(
    async (id) => {
      if (id === activeId && streamRef.current) {
        streamRef.current.cancel();
        streamRef.current = null;
        setIsGenerating(false);
      }
      try {
        await deleteConversation(id);
        if (id === activeId) {
          setActiveId(null);
          persistActive(null);
          setMessages([]);
        }
        await refreshList();
      } catch (e) {
        setError(e.message);
      }
    },
    [activeId, refreshList]
  );

  // Clear the active conversation on refresh if it no longer exists.
  const bootstrap = useCallback(async () => {
    const list = await refreshList();
    const stored = readActive();
    if (stored) {
      const stillThere = list.some((c) => c.conversation_id === stored);
      if (stillThere) {
        await loadConversation(stored);
      } else {
        persistActive(null);
      }
    }
  }, [refreshList, loadConversation]);

  const send = useCallback(
    async (text) => {
      const trimmed = (text || '').trim();
      if (!trimmed || isGenerating) return;

      // Optimistically append the user message.
      const userMsg = {
        id: `local-${Date.now()}`,
        role: 'user',
        content: trimmed,
        created_at: new Date().toISOString(),
      };

      let targetId = activeId;
      if (!targetId) {
        // No active conversation: create one first (existing API contract).
        try {
          const conv = await createConversation();
          targetId = conv.conversation_id;
          setActiveId(targetId);
          persistActive(targetId);
          await refreshList();
        } catch (e) {
          setError(e.message);
          return;
        }
      }

      setMessages((prev) => [...prev, userMsg]);
      setError(null);
      setIsGenerating(true);

      // Assistant placeholder that accumulates streamed text.
      const assistantMsg = {
        id: `assistant-${Date.now()}`,
        role: 'assistant',
        content: '',
        created_at: new Date().toISOString(),
        streaming: true,
      };
      setMessages((prev) => [...prev, assistantMsg]);

      const updateAssistant = (patch) =>
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantMsg.id ? { ...m, ...patch } : m))
        );

      const { stream, cancel, requestId } = streamChat(targetId, trimmed);
      streamRef.current = { cancel, requestId, assistantId: assistantMsg.id };

      try {
        const { full, terminal } = await consumeStream(stream, {
          onConversation: (cid) => {
            // Server-created conversation (first message, no id sent):
            // adopt the id so sidebar/refresh stay consistent.
            if (cid && cid !== targetId) {
              targetId = cid;
              setActiveId(cid);
              persistActive(cid);
              refreshList();
            }
          },
          onDelta: (delta) => {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantMsg.id
                  ? { ...m, content: m.content + delta }
                  : m
              )
            );
          },
          onDone: (t) => {
            // Authoritative final text — replaces accumulated deltas so the
            // response is never duplicated or truncated.
            updateAssistant({
              content: t.content,
              streaming: false,
              error: null,
              cancelled: false,
            });
          },
          onCancelled: (t) => {
            updateAssistant({
              content: t.content,
              streaming: false,
              cancelled: true,
            });
          },
          onError: (t) => {
            updateAssistant({ error: t.message, streaming: false });
            setError(t.message);
          },
        });

        // Local transport abort fallback (e.g. cancel POST raced the stream):
        // preserve the partial text exactly like a server-side cancellation.
        if (terminal.kind === 'aborted') {
          updateAssistant({
            content: terminal.content || full,
            streaming: false,
            cancelled: true,
          });
        }
      } catch (err) {
        updateAssistant({
          error: err?.message || 'Generation failed',
          streaming: false,
        });
        setError(err?.message || 'Generation failed');
        if (err?.status === 401) setNeedsAuth(true);
      } finally {
        streamRef.current = null;
        setIsGenerating(false);
        // Refresh list metadata only (title/order). Messages are NOT refetched
        // wholesale — that would replace streamed content and can duplicate
        // the final assistant response.
        refreshList();
      }
    },
    [activeId, isGenerating, refreshList]
  );

  const stop = useCallback(async () => {
    const s = streamRef.current;
    if (!s) return;
    streamRef.current = null;
    // Ask the server to cancel the in-flight generation by request_id.
    // The stream then emits its `cancelled` terminal frame carrying the
    // partial text; the UI preserves it and re-enables the composer.
    try {
      await cancelChat(s.requestId);
    } catch {
      /* already gone — fall through to local abort below */
    }
    // Safety net: if the terminal frame is somehow not delivered, abort the
    // transport locally; consumeStream maps this to a preserved partial.
    setTimeout(() => {
      try {
        s.cancel();
      } catch {
        /* noop */
      }
    }, 1500);
  }, []);

  // Retry: re-send the last user message (its failed assistant bubble is
  // replaced). Only offered when the trailing assistant message errored.
  const retry = useCallback(() => {
    if (isGenerating) return;
    const lastUser = [...messages].reverse().find((m) => m.role === 'user');
    const lastAssistant = [...messages].reverse().find(
      (m) => m.role === 'assistant'
    );
    if (!lastUser || !lastAssistant || !lastAssistant.error) return;
    setMessages((prev) => prev.filter((m) => m.id !== lastAssistant.id));
    send(lastUser.content).catch(() => {});
  }, [messages, isGenerating, send]);

  const dismissError = useCallback(() => setError(null), []);

  return {
    conversations,
    activeId,
    messages,
    isGenerating,
    error,
    needsAuth,
    status,
    setActiveId,
    loadConversation,
    newChat,
    removeConversation,
    send,
    stop,
    retry,
    refreshList,
    refreshStatus,
    bootstrap,
    dismissError,
  };
}
