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
  listChatProviders,
  updateConversationSettings,
  newRequestId,
  listWorkspaces as listWorkspacesApi,
  attachWorkspace as attachWorkspaceApi,
  listBots as listBotsApi,
  respondTask as respondTaskApi,
  getTask,
} from '../lib/api';
import { consumeStream } from '../lib/streaming';
import { createTextStreamSmoother } from '../lib/smoothStreaming';
import { sanitizeAssistantText, sanitizeConversation } from '../lib/sanitize';
import { reconcileTranscript } from '../lib/transcript';

const ACTIVE_KEY = 'kyrex-chat.activeConversationId';

// Minimum interval between FOCUS-triggered Bot-roster refreshes. Turn
// boundaries and explicit mutations refresh unthrottled; this only stops rapid
// focus/visibility churn (alt-tabbing) from spamming the API — there is no
// idle polling loop.
const BOTS_REFRESH_MIN_MS = 10000;
const CONVERSATION_REFRESH_INTERVAL_MS = 30000;

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
  // Server-registered workspaces (ids/names only — never filesystem paths)
  // and the workspace attached to the ACTIVE conversation. pendingWorkspaceId
  // holds a selection made before any conversation exists; it is sent with
  // the first message, after which the server persists the binding.
  const [workspaces, setWorkspaces] = useState([]);
  const [activeWorkspaceId, setActiveWorkspaceId] = useState(null);
  const pendingWorkspaceRef = useRef(null);
  // Bots visible to the authenticated user (discovery endpoint) and the Bot
  // bound to the ACTIVE conversation. The binding lives on the stored
  // conversation (server-authoritative) and is read back on every load, so
  // a refresh restores the same Bot. It is never inferred from the client.
  const [bots, setBots] = useState([]);
  const [activeBotId, setActiveBotId] = useState(null);
  // Latest active Bot binding without making refreshBots depend on it (so the
  // focus/turn callers keep a STABLE callback identity and the effect that
  // binds the focus listener never re-subscribes). Also the throttle clock.
  const activeBotIdRef = useRef(activeBotId);
  activeBotIdRef.current = activeBotId;
  const lastBotsRefreshRef = useRef(0);
  // Monotonic request-sequence for /api/bots. Only the NEWEST in-flight roster
  // request may update state; an earlier request that resolves after a newer
  // one is discarded, so a stale list can never restore a Bot a newer refresh
  // already observed as deleted.
  const botsRequestSeqRef = useRef(0);
  const [providers, setProviders] = useState([]);
  const [activeProvider, setActiveProvider] = useState(null);
  const [activeModel, setActiveModel] = useState(null);
  const streamRef = useRef(null); // { cancel, requestId, assistantId }
  const activeIdRef = useRef(activeId);
  activeIdRef.current = activeId;
  const transcriptRevisionRef = useRef(0);
  const transcriptRequestRef = useRef(0);
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  // Latest provider list without making loadConversation's identity depend on
  // the `providers` state. A `providers` dependency made loadConversation's
  // identity churn on every refreshProviders() call (fresh array each cycle),
  // which churned bootstrap's identity and re-fired App's `useEffect
  // (…, [bootstrap])` — an infinite refetch loop that re-loaded the stored
  // conversation every iteration, replacing (and visually flashing) the
  // message bubbles. The ref keeps the same fallback behavior with a stable
  // callback identity.
  const providersRef = useRef(providers);
  providersRef.current = providers;

  const refreshList = useCallback(async ({ silent = false } = {}) => {
    try {
      const list = await listConversations();
      setConversations(list);
      setNeedsAuth(false);
      return list;
    } catch (e) {
      if (!silent) setError(e.message);
      if (e.status === 401) setNeedsAuth(true);
      return [];
    }
  }, []);

  // Background Bot work can update a conversation while this page is open.
  // Refresh the sidebar periodically while visible, and immediately when the
  // user returns, so its latest-update preview replaces a stale "Ready" label.
  // Keep background failures quiet so they do not interrupt an active chat.
  useEffect(() => {
    let lastRefresh = 0;
    const refreshVisible = () => {
      if (document.visibilityState !== 'visible') return;
      const now = Date.now();
      if (now - lastRefresh < 2000) return;
      lastRefresh = now;
      refreshList({ silent: true });
    };
    const timer = window.setInterval(
      refreshVisible, CONVERSATION_REFRESH_INTERVAL_MS);
    window.addEventListener('focus', refreshVisible);
    document.addEventListener('visibilitychange', refreshVisible);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('focus', refreshVisible);
      document.removeEventListener('visibilitychange', refreshVisible);
    };
  }, [refreshList]);

  const refreshWorkspaces = useCallback(async () => {
    try {
      setWorkspaces(await listWorkspacesApi());
    } catch {
      setWorkspaces([]); // registry listing is best-effort; pure chat still works
    }
  }, []);

  const refreshProviders = useCallback(async () => {
    try { setProviders(await listChatProviders()); } catch { setProviders([]); }
  }, []);

  // Refresh the Bot roster from the authoritative `/api/bots` endpoint and
  // reconcile the ACTIVE selection against it. The backend is the sole
  // authority (it reloads the registry on every call), so a Bot deleted
  // elsewhere disappears here WITHOUT a full page reload.
  //
  // Reconcile rule: a selection whose Bot STILL EXISTS is preserved exactly; a
  // selection whose Bot was DELETED is cleared so the UI falls back cleanly
  // (the deleted id is no longer a valid option and the backend would reject a
  // turn for it anyway). The binding on the stored conversation is untouched —
  // this only drops a phantom client-side selection.
  //
  // `throttle` is used by the focus/visibility boundary so rapid focus changes
  // never spam the API; bootstrap, mutations, and turn boundaries pass no
  // throttle and always refetch.
  const refreshBots = useCallback(async ({ throttle = false } = {}) => {
    const now = Date.now();
    if (throttle && now - lastBotsRefreshRef.current < BOTS_REFRESH_MIN_MS) {
      return null;
    }
    lastBotsRefreshRef.current = now;
    // Request-sequence guard. Capture this request's slot; a later call
    // advances the counter, so ONLY the newest response is allowed to touch
    // bots/activeBotId. This makes a slower, older response a no-op — it can
    // never resurrect a deleted Bot or re-select a cleared one.
    const seq = ++botsRequestSeqRef.current;
    let list;
    try {
      list = await listBotsApi();
    } catch {
      if (seq !== botsRequestSeqRef.current) return null; // superseded — drop
      setBots([]); // discovery is best-effort; ordinary chat still works
      return [];
    }
    if (seq !== botsRequestSeqRef.current) return null; // superseded — drop
    setBots(list);
    const current = activeBotIdRef.current;
    if (current && !list.some((b) => b && String(b.id) === String(current))) {
      setActiveBotId(null);
    }
    return list;
  }, []);

  // Sensible turn/focus boundaries for the roster refresh (never a poll):
  //   * returning to the tab (window focus / visibilitychange) — throttled;
  //   * the end of every chat turn (see `send`'s finally) — unthrottled, one
  //     call per turn.
  // This is what makes a Bot deleted in another tab/session vanish from the
  // picker, Bot settings, and sidebar attribution without a page reload.
  useEffect(() => {
    const onBoundary = () => { refreshBots({ throttle: true }); };
    const onVisibility = () => {
      if (document.visibilityState === 'visible') onBoundary();
    };
    window.addEventListener('focus', onBoundary);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.removeEventListener('focus', onBoundary);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [refreshBots]);

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
    const sameConversation = activeIdRef.current === id;
    if (sameConversation && streamRef.current) return;
    const revision = ++transcriptRevisionRef.current;
    const request = ++transcriptRequestRef.current;
    // Switching conversations during generation cancels the in-flight turn
    // so the stream can never append into the wrong conversation view.
    if (streamRef.current) {
      streamRef.current.cancel();
      streamRef.current.cancelSmoother?.();
      streamRef.current = null;
      setIsGenerating(false);
    }
    activeIdRef.current = id;
    setActiveId(id);
    persistActive(id);
    if (!sameConversation) setMessages([]);
    setError(null);
    try {
      const conv = await getConversation(id);
      if (activeIdRef.current !== id || revision !== transcriptRevisionRef.current) return;
      // Presentation boundary: stored assistant text is sanitized on load so a
      // conversation persisted before/outside the sanitizer still renders
      // without internal control markers.
      const incoming = sanitizeConversation(conv).messages || [];
      if (request === transcriptRequestRef.current) {
        setMessages(current => sameConversation ? reconcileTranscript(current, incoming) : incoming);
      }
      setActiveWorkspaceId(conv.workspace_id || null);
      // The stored binding is authoritative — never inferred client-side.
      setActiveBotId(conv.bot_id || null);
      const fallback = providersRef.current[0];
      setActiveProvider(conv.provider || (!conv.bot_id ? fallback?.id : null));
      setActiveModel(conv.model || (!conv.bot_id ? fallback?.models?.[0] : null));
      pendingWorkspaceRef.current = null;
    } catch (e) {
      if (activeIdRef.current === id && revision === transcriptRevisionRef.current
          && request === transcriptRequestRef.current) setError(e.message);
    }
  }, []);

  // Re-fetch the conversation's messages from the server WITHOUT touching any
  // other state. Used by the Delegated Work poller to reflect a just-relayed
  // terminal result in the open transcript. Never runs while a generation
  // stream is live (that would clobber the streaming bubble) — the caller
  // polls only when idle, and this guards defensively via streamRef.
  const refreshMessages = useCallback(async (id) => {
    const target = id || activeId;
    if (!target || streamRef.current) return;
    const revision = transcriptRevisionRef.current;
    const request = ++transcriptRequestRef.current;
    try {
      // An SSE failure closes only the viewer, not the durable task. Read its
      // status before the transcript so a terminal task can publish its saved
      // reply through the existing conversation recovery path.
      const statuses = new Map();
      await Promise.all(messagesRef.current.filter(m => m.connection_interrupted && m.task?.taskId)
        .map(async m => {
          try {
            const task = await getTask(m.task.taskId);
            statuses.set(m.task.taskId, task.status || 'unknown');
          } catch {
            statuses.set(m.task.taskId, 'unknown');
          }
        }));
      let incoming = null;
      try {
        const conv = await getConversation(target);
        incoming = sanitizeConversation(conv).messages || [];
      } catch {
        // A transcript read can fail after a successful task-status read.
        // Keep the last visible text and still apply the verified status.
      }
      if (streamRef.current || activeIdRef.current !== target
          || revision !== transcriptRevisionRef.current
          || request !== transcriptRequestRef.current) return;
      setMessages(current => {
        const updated = current.map(m =>
          m.connection_interrupted && statuses.has(m.task?.taskId)
            ? { ...m, task: { ...m.task, status: statuses.get(m.task.taskId) } }
            : m);
        return incoming ? reconcileTranscript(updated, incoming) : updated;
      });
    } catch {
      /* best-effort: the Delegated Work card still shows the status */
    }
  }, [activeId]);

  // Check immediately after a lost stream; the existing visible-page poll
  // and focus/online boundaries then follow the SAME task without resending.
  const interruptedTasksKey = messages.filter(m => m.connection_interrupted)
    .map(m => m.task?.taskId).join(',');
  useEffect(() => {
    if (!isGenerating && messagesRef.current.some(m => m.connection_interrupted)) {
      refreshMessages(activeId);
    }
  }, [activeId, isGenerating, refreshMessages, interruptedTasksKey]);

  // Completed research can publish a final answer while this page is idle.
  // Re-fetch the active transcript without replacing an in-flight stream.
  useEffect(() => {
    if (!activeId) return;
    const refresh = () => {
      if (document.visibilityState === 'visible') refreshMessages(activeId);
    };
    const timer = window.setInterval(refresh, 10000);
    window.addEventListener('focus', refresh);
    window.addEventListener('online', refresh);
    document.addEventListener('visibilitychange', refresh);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('focus', refresh);
      window.removeEventListener('online', refresh);
      document.removeEventListener('visibilitychange', refresh);
    };
  }, [activeId, refreshMessages]);

  // Start a conversation, optionally bound to a Bot. botId=null/undefined
  // creates ordinary Kyrex Chat. The server validates and persists the
  // binding; selecting a different Bot always creates a NEW conversation and
  // never mutates the binding of an existing one.
  const newChat = useCallback(
    async (botId) => {
      if (streamRef.current) {
        streamRef.current.cancel();
        streamRef.current.cancelSmoother?.();
        streamRef.current = null;
        setIsGenerating(false);
      }
      try {
        const conv = await createConversation(botId || undefined);
        setActiveId(conv.conversation_id);
        persistActive(conv.conversation_id);
        setMessages([]);
        setActiveWorkspaceId(null); // binding starts empty; pending selection applies on first send
        setActiveBotId(conv.bot_id || null);
        setActiveProvider(!conv.bot_id ? (providers[0]?.id || null) : null);
        setActiveModel(!conv.bot_id ? (providers[0]?.models?.[0] || null) : null);
        setError(null);
        await refreshList();
        return conv;
      } catch (e) {
        setError(e.message);
        return null;
      }
    },
    [refreshList, providers]
  );

  const removeConversation = useCallback(
    async (id) => {
      if (id === activeId && streamRef.current) {
        streamRef.current.cancel();
        streamRef.current.cancelSmoother?.();
        streamRef.current = null;
        setIsGenerating(false);
      }
      try {
        await deleteConversation(id);
        if (id === activeId) {
          setActiveId(null);
          persistActive(null);
          setMessages([]);
          setActiveBotId(null);
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
    refreshProviders();
    refreshBots();
    const stored = readActive();
    if (stored) {
      const stillThere = list.some((c) => c.conversation_id === stored);
      if (stillThere) {
        await loadConversation(stored);
      } else {
        persistActive(null);
      }
    }
  }, [refreshList, loadConversation, refreshBots]);

  const changeProvider = useCallback(async (provider, model) => {
    if (!activeId || activeBotId) return;
    try {
      const conv = await updateConversationSettings(activeId, provider, model);
      setActiveProvider(conv.provider || provider);
      setActiveModel(conv.model || model);
    } catch (e) { setError(e.message); }
  }, [activeId, activeBotId]);

  const send = useCallback(
    async (text) => {
      const trimmed = (text || '').trim();
      if (!trimmed || isGenerating) return;
      transcriptRevisionRef.current += 1;
      const turnRequestId = newRequestId();

      // Optimistically append the user message.
      const userMsg = {
        id: `turn-${turnRequestId}-user`,
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
        id: `turn-${turnRequestId}-assistant`,
        turn_user_id: userMsg.id,
        role: 'assistant',
        content: '',
        created_at: new Date().toISOString(),
        streaming: true,
        task: null,
        events: [],
        approval: null,
      };
      setMessages((prev) => [...prev, assistantMsg]);

      const updateAssistant = (patch) =>
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantMsg.id ? { ...m, ...patch } : m))
        );
      let durableTaskId = null;
      const recoverTask = (content) => updateAssistant({
        ...(typeof content === 'string' ? { content } : {}),
        streaming: false,
        error: null,
        connection_interrupted: true,
        task: { taskId: durableTaskId, status: 'unknown' },
      });

      const smoother = createTextStreamSmoother((content) => {
        updateAssistant({ content });
      });

      // Workspace for this turn: the conversation's stored binding wins;
      // otherwise a pre-selection made before the conversation existed.
      const wsForTurn = activeWorkspaceId || pendingWorkspaceRef.current || null;
      const { stream, cancel, requestId } = streamChat(
        targetId, trimmed, turnRequestId, wsForTurn || undefined);
      streamRef.current = {
        cancel, requestId, assistantId: assistantMsg.id,
        cancelSmoother: () => smoother.cancel(),
      };

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
            smoother.push(delta);
          },
          onDone: (t) => {
            // If an intermediate stream frame set the page-level error but
            // the authoritative terminal frame is successful, clear the
            // stale banner along with the message-level error.
            setError(null);
            // Authoritative final text — replaces accumulated deltas so the
            // response is never duplicated or truncated.
            smoother.finish(t.content, (content) => {
              updateAssistant({
                content,
                streaming: false,
                error: null,
                cancelled: false,
                approval: null,
              });
            });
          },
          onCancelled: (t) => {
            smoother.finish(t.content, (content) => {
              updateAssistant({
                content,
                streaming: false,
                cancelled: true,
                approval: null,
              });
            });
          },
          onError: (t) => {
            smoother.cancel();
            updateAssistant({ error: t.message, streaming: false, approval: null });
            setError(t.message);
          },
          onMessageSend: (event) => { updateAssistant({ message_send: { id: event.send_id } }); },
          onTask: (t) => {
            durableTaskId = t.task_id || durableTaskId;
            updateAssistant({
              task: { taskId: t.task_id, status: t.status },
              persisted_id: `task-${t.task_id}-result`,
            });
          },
          onProgress: (p) => {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantMsg.id
                  ? { ...m, events: [...(m.events || []), { kind: 'progress', payload: p }].slice(-100) }
                  : m
              )
            );
          },
          onApprovalRequest: (req) => {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantMsg.id
                  ? {
                      ...m,
                      approval: req,
                      events: [...(m.events || []), { kind: 'approval_request', ...req }],
                    }
                  : m
              )
            );
          },
          onApprovalResult: (res) => {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantMsg.id
                  ? {
                      ...m,
                      approval: null,
                      events: [...(m.events || []), { kind: 'approval_result', ...res }],
                    }
                  : m
              )
            );
          },
        });
        await smoother.whenIdle();

        // Writable Bot tasks complete through the durable-task stream, which
        // emits a terminal done frame rather than incremental text. Apply it
        // here so the placeholder never remains in the typing state.
        if (terminal.kind === 'done') {
          setError(null);
          updateAssistant({
            content: terminal.content || sanitizeAssistantText(full),
            ...(terminal.developer_result ? { developer_result: true, events: terminal.events } : {}),
            streaming: false,
            error: null,
            cancelled: false,
            approval: null,
            task: null,
          });
        } else
        // Local transport abort fallback (e.g. cancel POST raced the stream):
        // preserve the partial text exactly like a server-side cancellation.
        if (terminal.kind === 'aborted' || terminal.kind === 'cancelled') {
          updateAssistant({
            content: terminal.content || sanitizeAssistantText(full),
            streaming: false,
            cancelled: true,
            task: null,
            approval: null,
          });
        } else if (terminal.kind === 'error') {
          smoother.cancel();
          if (durableTaskId) recoverTask(full);
          else {
            updateAssistant({
              content: full,
              streaming: false,
              error: terminal.message || 'Stream error',
            });
            setError(terminal.message || 'Stream error');
          }
        }
      } catch (err) {
        smoother.cancel();
        if (durableTaskId) recoverTask();
        else {
          updateAssistant({
            error: err?.message || 'Generation failed',
            streaming: false,
          });
          setError(err?.message || 'Generation failed');
          if (err?.status === 401) setNeedsAuth(true);
        }
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
        refreshList({ silent: true });
        // Turn boundary: refresh the Bot roster too, so a Bot created/deleted
        // while this turn ran is reflected in the picker and sidebar at once.
        refreshBots();
      }
    },
    [activeId, isGenerating, activeWorkspaceId, refreshList, refreshBots]
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
  // Approve/deny a pending Bot-task approval. The reply is recorded against
  // the exact task (server-scoped by task_id + chat ownership) so it can
  // never resolve a different Bot's or conversation's approval.
  const respondApproval = useCallback(async (taskId, text, assistantId) => {
    if (!taskId || !text) return;
    try {
      await respondTaskApi(taskId, text);
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId ? { ...m, approval: null } : m
        )
      );
    } catch (e) {
      setError(e.message);
    }
  }, []);

  const retry = useCallback(() => {
    if (isGenerating) return;
    const lastUser = [...messages].reverse().find((m) => m.role === 'user');
    const lastAssistant = [...messages].reverse().find(
      (m) => m.role === 'assistant'
    );
    if (lastAssistant?.connection_interrupted) {
      return refreshMessages(activeId);
    }
    if (!lastUser || !lastAssistant || !lastAssistant.error) return;
    setMessages((prev) => prev.filter((m) => m.id !== lastAssistant.id));
    send(lastUser.content).catch(() => {});
  }, [messages, isGenerating, send, refreshMessages, activeId]);

  const dismissError = useCallback(() => setError(null), []);

  return {
    conversations,
    activeId,
    messages,
    isGenerating,
    error,
    needsAuth,
    workspaces,
    activeWorkspaceId,
    attachWorkspace,
    refreshWorkspaces,
    bots,
    activeBotId,
    providers,
    refreshProviders,
    activeProvider,
    activeModel,
    changeProvider,
    refreshBots,
    setActiveId,
    loadConversation,
    refreshMessages,
    newChat,
    removeConversation,
    send,
    stop,
    retry,
    respondApproval,
    refreshList,
    bootstrap,
    dismissError,
  };
}
