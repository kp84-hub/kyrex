import React, { useEffect, useState } from 'react';
import { useChat } from './hooks/useChat.js';
import Sidebar from './components/Sidebar.jsx';
import ChatHeader from './components/ChatHeader.jsx';
import MessageList from './components/MessageList.jsx';
import Composer from './components/Composer.jsx';
import ProviderSettings from './components/ProviderSettings.jsx';
import ConnectionsSettings from './components/ConnectionsSettings.jsx';
import BotSettings from './components/BotSettings.jsx';
import DelegatedWork from './components/DelegatedWork.jsx';
import { fetchDelegations } from './lib/api.js';
import { delegationsNeedPolling } from './lib/delegations.js';

import {
  activeSubscriptions,
  buildActivityLines,
  isTerminalActivity,
} from './lib/activeWork.js';
import { useLiveActivity } from './hooks/useLiveActivity.js';

export default function App() {  const {
    conversations,
    activeId,
    messages,
    isGenerating,
    error,
    needsAuth,
    status,
    loadConversation,
    refreshMessages,
    newChat,
    removeConversation,
    send,
    stop,
    retry,
    respondApproval,
    refreshStatus,
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
  // Read-only "Delegated work" rows for the active conversation. Refreshed when
  // the conversation changes, when a turn finishes, and — while any delegation
  // is still NON-TERMINAL — by a bounded poll, so a target that finishes while
  // the conversation sits idle still moves the card to its final result. The
  // poll stops as soon as every delegation is terminal (no idle refresh loop).
  const [delegations, setDelegations] = useState([]);

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
        // A terminal result was just relayed into the stored conversation:
        // reflect it in the open transcript (never while a turn is streaming).
        if (relayed && relayed.length && !isGenerating) {
          refreshMessages(activeId);
        }
        // Keep polling ONLY while something is still running and no turn is in
        // flight. Once every row is terminal the loop ends for good.
        if (delegationsNeedPolling(rows) && !isGenerating && !cancelled) {
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
  }, [activeId, isGenerating, refreshMessages]);

  // Restore the conversation list (and the previously selected conversation)
  // after a browser refresh; re-probe engine availability.
  useEffect(() => {
    bootstrap();
  }, [bootstrap]);

  useEffect(() => {
    refreshStatus();
  }, [refreshStatus]);

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
  // EXCLUSIVELY from durable task/delegation state plus the live SSE/Flux
  // status — never from model output. No polling is added: background tasks
  // are followed over the existing Flux event stream, and the active turn
  // reuses chat state already in memory.
  const activitySubs = activeSubscriptions(conversations);
  const liveFlux = useLiveActivity(activitySubs);

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
      };
    }
    if (isGenerating) {
      const lastUser = [...messages].reverse().find((m) => m.role === 'user');
      return {
        kind: 'task',
        status: 'running',
        text: lastUser ? lastUser.content : '',
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
        onSettings={() => { setSettingsOpen(true); setBotsOpen(false); setSidebarOpen(false); }}
        onBots={() => { setBotsOpen(true); setSettingsOpen(false); setSidebarOpen(false); }}
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
          status={status}
          workspaces={workspaces}
          activeWorkspaceId={activeWorkspaceId}
          onAttachWorkspace={attachWorkspace}
          onToggleSidebar={() => setSidebarOpen((o) => !o)}
          bots={bots}
          activeBotId={activeBotId}
          onSelectBot={startNewChatWithBot}
        />
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
          <>
            <ProviderSettings onClose={() => setSettingsOpen(false)} onSaved={refreshProviders} />
            <ConnectionsSettings />
          </>
        ) : botsOpen ? (
          <BotSettings
            bots={bots}
            onClose={() => setBotsOpen(false)}
            onChanged={refreshBots}
          />
        ) : (
        <div className="chat-area">
          <DelegatedWork delegations={delegations} />
          <MessageList
            messages={messages}
            isGenerating={isGenerating}
            onRetry={retry}
            onRespondApproval={respondApproval}
          />
          <Composer onSend={send} onStop={stop} isGenerating={isGenerating} providers={providers} activeProvider={activeProvider} activeModel={activeModel} activeBotId={activeBotId} onChangeProvider={changeProvider} />
        </div>
        )}
      </main>
    </div>
  );
}
