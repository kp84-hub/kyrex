import React, { useEffect, useState } from 'react';
import { useChat } from './hooks/useChat.js';
import Sidebar from './components/Sidebar.jsx';
import ChatHeader from './components/ChatHeader.jsx';
import MessageList from './components/MessageList.jsx';
import Composer from './components/Composer.jsx';

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
    refreshStatus,
    bootstrap,
    dismissError,
    workspaces,
    activeWorkspaceId,
    attachWorkspace,
  } = useChat();

  const [sidebarOpen, setSidebarOpen] = useState(false);

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

  return (
    <div className="app">
      <Sidebar
        conversations={conversations}
        activeId={activeId}
        onSelect={selectConversation}
        onNew={startNewChat}
        onDelete={removeConversation}
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
        <div className="chat-area">
          <MessageList
            messages={messages}
            isGenerating={isGenerating}
            onRetry={retry}
          />
          <Composer onSend={send} onStop={stop} isGenerating={isGenerating} />
        </div>
      </main>
    </div>
  );
}
