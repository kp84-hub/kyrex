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
  listWorkspaces as listWorkspacesApi,
  attachWorkspace as attachWorkspaceApi,
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
  // Server-registered workspaces (ids/names only — never filesystem paths)
  // and the workspace attached to the ACTIVE conversation. pendingWorkspaceId
  // holds a selection made before any conversation exists; it is sent with
  // the first message, after which the server persists the binding.
  const [workspaces, setWorkspaces] = useState([]);
  const [activeWorkspaceId, setActiveWorkspaceId] = useState(null);
  const pendingWorkspaceRef = useRef(null);
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

  const refreshWorkspaces = useCallback(async () => {
    try {
      setWorkspaces(await listWorkspacesApi());
    } catch {
      setWorkspaces([]); // registry listing is best-effort; pure chat still works
    }
  }, []);

  // Attach (or detach with null) a registered workspace. With an active
  // conversation the binding is persisted server-side immediately; otherwise
  // the selection is held and sent with the first message of the next chat.
  const attachWorkspace = useCallback(
    async (id) => {
      if (id) pendingWorkspaceRef.current = id;
      else pendingWorkspaceRef.current = null;
      if (activeId) {
        try {
          const r = await attachWorkspaceApi(activeId, id);
          setActiveWorkspaceId(r.workspace_id || null);
        } catch (e) {
          setError(e.message);
        }
      } else {
        setActiveWorkspaceId(id || null);
      }
    },
    [activeId]
  );

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
      setActiveWorkspaceId(conv.workspace_id || null);
      pendingWorkspaceRef.current = null;
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
      setActiveWorkspaceId(null); // binding starts empty; pending selection applies on first send
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
    refreshWorkspaces();
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

      // Workspace for this turn: the conversation's stored binding wins;
      // otherwise a pre-selection made before the conversation existed.
      const wsForTurn = activeWorkspaceId || pendingWorkspaceRef.current || null;
      const { stream, cancel, requestId } = streamChat(
        targetId, trimmed, undefined, wsForTurn || undefined);
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
        // The server persisted the workspace binding for this turn (if one
        // was sent); adopt it and clear any pre-conversation selection.
        if (wsForTurn) {
          setActiveWorkspaceId(wsForTurn);
          pendingWorkspaceRef.current = null;
        }
        // Refresh list metadata only (title/order). Messages are NOT refetched
        // wholesale — that would replace streamed content and can duplicate
        // the final assistant response.
        refreshList();
      }
    },
    [activeId, isGenerating, activeWorkspaceId, refreshList]
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
    workspaces,
    activeWorkspaceId,
    attachWorkspace,
    refreshWorkspaces,
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
