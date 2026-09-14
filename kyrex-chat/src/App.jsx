import React, { useEffect, useState } from 'react';
import { useChat } from './hooks/useChat.js';
import Sidebar from './components/Sidebar.jsx';
import ChatHeader from './components/ChatHeader.jsx';
import MessageList from './components/MessageList.jsx';
import Composer from './components/Composer.jsx';
import ProviderSettings from './components/ProviderSettings.jsx';
import BotSettings from './components/BotSettings.jsx';
import DelegatedWork from './components/DelegatedWork.jsx';
import { fetchDelegations } from './lib/api.js';

export default function App() {
  const {
    conversations,
    activeId,
    messages,
    isGenerating,
    error,
    needsAuth,
    status,
    loadConversation,
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
  // the conversation changes and when a turn finishes (the coordinator may have
  // created or progressed delegations during the turn). Status-only.
  const [delegations, setDelegations] = useState([]);

  useEffect(() => {
    let cancelled = false;
    if (!activeId) {
      setDelegations([]);
      return () => { cancelled = true; };
    }
    fetchDelegations(activeId)
      .then((rows) => { if (!cancelled) setDelegations(rows); })
      .catch(() => { if (!cancelled) setDelegations([]); });
    return () => { cancelled = true; };
  }, [activeId, isGenerating]);

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
          <ProviderSettings onClose={() => setSettingsOpen(false)} onSaved={refreshProviders} />
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
