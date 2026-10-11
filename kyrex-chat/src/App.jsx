import React, { useEffect, useRef, useState } from 'react';
import { useChat } from './hooks/useChat.js';
import { useAppInstall } from './hooks/useAppInstall.js';
import Sidebar from './components/Sidebar.jsx';
import ChatHeader from './components/ChatHeader.jsx';
import MessageList from './components/MessageList.jsx';
import Composer from './components/Composer.jsx';
import ProviderSettings from './components/ProviderSettings.jsx';
import ConnectionsSettings from './components/ConnectionsSettings.jsx';
import BotSettings from './components/BotSettings.jsx';
import DelegatedWork from './components/DelegatedWork.jsx';
import { approveDelegatedTask, cancelTask, fetchDelegations, respondTask } from './lib/api.js';
import { delegationsNeedPolling } from './lib/delegations.js';

import {
  activeSubscriptions,
  buildActivityLines,
  isTerminalActivity,
} from './lib/activeWork.js';
import { useLiveActivity } from './hooks/useLiveActivity.js';
import { latestProgressStage } from './lib/progress.js';

export default function App() {
  const installState = useAppInstall();
  const {
    conversations,
    activeId,
    messages,
    isGenerating,
    error,
    needsAuth,
    loadConversation,
    refreshMessages,
    refreshList,
    newChat,
    removeConversation,
    send,
    stop,
    retry,
    respondApproval,
    bootstrap,
    dismissError,
    workspaces,
    activeWorkspaceId,
    attachWorkspace,
    bots,
    activeBotId,
    refreshBots,
    providers,
    refreshProviders,
    activeProvider,
    activeModel,
    changeProvider,
  } = useChat();

  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [botsOpen, setBotsOpen] = useState(false);
  const [connectionsOpen, setConnectionsOpen] = useState(false);
  // Read-only "Delegated work" rows for the active conversation. Refreshed when
  // the conversation changes, when a turn finishes, and — while any delegation
  // is still NON-TERMINAL — by a bounded poll, so a target that finishes while
  // the conversation sits idle still moves the card to its final result. The
  // poll stops as soon as every delegation is terminal (no idle refresh loop).
  const [delegations, setDelegations] = useState([]);
  const deferredDelegationRefresh = useRef(null);
  const knownTargetThreads = useRef(new Set());

  useEffect(() => {
    let cancelled = false;
    let timer = null;

    const load = async () => {
      if (!activeId) {
        setDelegations([]);
        return;
      }
      try {
        const { delegations: rows, relayed } = await fetchDelegations(activeId);
        if (cancelled) return;
        setDelegations(rows);
        const newThreads = rows.filter(d => d.target_conversation_id
          && !knownTargetThreads.current.has(d.target_conversation_id));
        if (newThreads.length) {
          newThreads.forEach(d => knownTargetThreads.current.add(d.target_conversation_id));
          refreshList({ silent: true });
        }
        if (relayed?.length && isGenerating) deferredDelegationRefresh.current = activeId;
        // A terminal result was just relayed into the stored conversation:
        // reflect it in the open transcript (never while a turn is streaming).
        if (!isGenerating && (relayed?.length || deferredDelegationRefresh.current === activeId)) {
          deferredDelegationRefresh.current = null;
          refreshMessages(activeId);
        }
        // Cards keep following the target even while the coordinator streams.
        // Transcript refresh still waits for that turn to finish.
        if (delegationsNeedPolling(rows) && !cancelled) {
          timer = setTimeout(load, 2500);
        }
      } catch {
        if (!cancelled) setDelegations([]);
      }
    };

    load();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [activeId, isGenerating, refreshMessages, refreshList]);

  const respondDelegatedApproval = async (taskId, text) => {
    await respondTask(taskId, text);
    // Remove stale controls immediately; bounded polling supplies the next
    // durable state without fabricating success.
    setDelegations((rows) => rows.map((row) =>
      row && row.task_id === taskId ? { ...row, approval: null } : row
    ));
  };

  // Delegated T2 Approve: the backend resolves THIS task's stored approval
  // token host-side, so no token is ever sent from (or returned to) the client.
  const approveDelegated = async (taskId) => {
    await approveDelegatedTask(taskId);
    setDelegations((rows) => rows.map((row) =>
      row && row.task_id === taskId ? { ...row, approval: null } : row
    ));
  };


  const cancelDelegatedTask = async (taskId) => {
    const result = await cancelTask(taskId);
    // Reflect the server's authoritative immediate state. A queued task becomes
    // cancelled immediately; running/approval work may remain active briefly
    // while the worker observes the cancellation request.
    setDelegations((rows) => rows.map((row) =>
      row && row.task_id === taskId
        ? { ...row, status: result.status || row.status, approval: null }
        : row
    ));
  };

  // Restore the conversation list (and the previously selected conversation)
  // after a browser refresh.
  useEffect(() => {
    bootstrap();
  }, [bootstrap]);

  // Selecting a conversation also closes the mobile drawer.
  const selectConversation = (id) => {
    loadConversation(id);
    setSidebarOpen(false);
  };

  const startNewChat = async () => {
    await newChat();
    setSidebarOpen(false);
  };

  // Selecting a Bot in the header ALWAYS starts a new Bot-bound conversation
  // (server-validated) — it never mutates the binding of an existing one.
  const startNewChatWithBot = async (botId) => {
    await newChat(botId);
    setSidebarOpen(false);
  };

  // ── Sidebar active-work line ───────────────────────────────────────────
  // One concise, visually secondary line per open bot chat, derived
  // from durable task/delegation state plus the latest SSE/Flux progress
  // stage. No polling is added: background tasks
  // are followed over the existing Flux event stream, and the active turn
  // reuses chat state already in memory.
  const activitySubs = activeSubscriptions(conversations);
  const liveFlux = useLiveActivity(activitySubs);
  const targetActivity = liveFlux[activeId];
  useEffect(() => {
    if (targetActivity && (isTerminalActivity(targetActivity.status)
      || targetActivity.status === 'awaiting_approval')) {
      refreshMessages(activeId);
    }
  }, [activeId, targetActivity?.task_id, targetActivity?.status, refreshMessages]);

  const visibleMessages = messages.map(message => {
    if (!message.delegated_active || message.task?.taskId !== targetActivity?.task_id) return message;
    const events = message.events || [];
    const stage = targetActivity.progress_update;
    return { ...message, task: { ...message.task, status: targetActivity.status },
      events: stage && stage !== latestProgressStage(events)
        ? [...events, { kind: 'progress', payload: { stage } }].slice(-100) : events };
  });
  const currentDelegations = delegations.filter(d =>
    !d.parent_conversation_id || d.parent_conversation_id === activeId);

  const activeConv = conversations.find((c) => c.conversation_id === activeId);
  const activeIsBot = Boolean(activeConv && activeConv.bot_id);

  // Immediate, reactive state for the ACTIVE conversation, so the line shows
  // during the turn (before the next list refresh): the delegations already
  // fetched for it, else the in-flight Bot task whose text is the user's own
  // (owner-typed) message. `undefined` defers to the durable descriptor.
  const activeActivity = (() => {
    if (!activeId || !activeIsBot) return undefined;
    const rows = (Array.isArray(delegations) ? delegations : []).filter(
      (d) =>
        d &&
        (!d.parent_conversation_id || d.parent_conversation_id === activeId)
    );
    const pending = rows.find((d) => d.status && !isTerminalActivity(d.status));
    if (pending) {
      return {
        kind: 'delegation',
        status: pending.status,
        task_id: pending.task_id,
        delegation_id: pending.delegation_id,
        target_bot_id: pending.target_bot_id,
        text: pending.text,
        progress_update: latestProgressStage((pending.progress || []).map(payload => ({ kind: 'progress', payload }))),
      };
    }
    if (isGenerating) {
      const lastUser = [...messages].reverse().find((m) => m.role === 'user');
      const lastAssistant = [...messages].reverse().find((m) => m.role === 'assistant');
      return {
        kind: 'task',
        status: lastAssistant?.approval || lastAssistant?.task?.status === 'awaiting_approval'
          ? 'awaiting_approval' : 'running',
        task_id: lastAssistant?.task?.taskId,
        text: lastUser ? lastUser.content : '',
        progress_update: latestProgressStage(lastAssistant?.events),
      };
    }
    // Delegations for this conversation all settled → clear the line now,
    // without waiting for the next list refresh.
    if (rows.length > 0) return null;
    return undefined;
  })();

  const live = { ...liveFlux };
  if (activeId && activeActivity !== undefined) live[activeId] = activeActivity;
  const activityLines = buildActivityLines(conversations, { bots, live });

  return (
    <div className="app">
      <Sidebar
        conversations={conversations}
        activeId={activeId}
        onSelect={selectConversation}
        onNew={startNewChat}
        onDelete={removeConversation}
        onSettings={() => { setSettingsOpen(true); setBotsOpen(false); setConnectionsOpen(false); setSidebarOpen(false); }}
        onBots={() => { setBotsOpen(true); setSettingsOpen(false); setConnectionsOpen(false); setSidebarOpen(false); }}
        onConnections={() => { setConnectionsOpen(true); setSettingsOpen(false); setBotsOpen(false); setSidebarOpen(false); }}
        bots={bots}
        activityLines={activityLines}
        open={sidebarOpen}
      />
      <div
        className={`backdrop ${sidebarOpen ? 'visible' : ''}`}
        onClick={() => setSidebarOpen(false)}
        aria-hidden="true"
      />
      <main className="main">
        <ChatHeader
          onToggleSidebar={() => setSidebarOpen((o) => !o)}
          bots={bots}
          activeBotId={activeBotId}
          onSelectBot={startNewChatWithBot}
        />
        {/* Provider configuration errors are surfaced through this banner.
            Provider configuration and workspace attachment are distinct
            states; provider-only must never be labelled “Engine ready”. */}
        {error && (
          <div className="banner" role="alert">
            <span className="banner-text">{error}</span>
            {needsAuth && (
              <a className="banner-link" href="/auth/login">
                Sign in with GitHub
              </a>
            )}
            <button
              type="button"
              className="banner-close"
              aria-label="Dismiss error"
              onClick={dismissError}
            >
              ×
            </button>
          </div>
        )}
        {settingsOpen ? (
          <ProviderSettings installState={installState} onClose={() => setSettingsOpen(false)} onSaved={refreshProviders} />
        ) : connectionsOpen ? (
          <ConnectionsSettings onClose={() => setConnectionsOpen(false)} />
        ) : botsOpen ? (
          <BotSettings
            bots={bots}
            onClose={() => setBotsOpen(false)}
            onChanged={refreshBots}
          />
        ) : (
        <div className="chat-area">
          <DelegatedWork
            delegations={currentDelegations}
            conversationId={activeId}
            onRespondApproval={respondDelegatedApproval}
            onApproveDelegated={approveDelegated}
            onCancelTask={cancelDelegatedTask}
            onOpenConversation={selectConversation}
          />
          <MessageList
            messages={visibleMessages}
            conversationId={activeId}
            isGenerating={isGenerating}
            onRetry={retry}
            onRespondApproval={respondApproval}
            onApproveDelegated={async taskId => {
              await approveDelegatedTask(taskId);
              refreshMessages(activeId);
            }}
            delegations={currentDelegations.map(d => {
              const latest = liveFlux[activeId];
              if (!latest || latest.task_id !== d.task_id) return d;
              return { ...d, status: latest.status,
                progress: latest.progress_update ? [{ stage: latest.progress_update }] : d.progress };
            })}
            onOpenConversation={selectConversation}
          />
          <Composer onSend={send} onStop={stop} isGenerating={isGenerating} providers={providers} activeProvider={activeProvider} activeModel={activeModel} activeBotId={activeBotId} onChangeProvider={changeProvider} workspaces={workspaces} activeWorkspaceId={activeWorkspaceId} onAttachWorkspace={attachWorkspace} />
        </div>
        )}
      </main>
    </div>
  );
}
