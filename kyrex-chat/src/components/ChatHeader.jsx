import React from 'react';
import BotPicker from './BotPicker.jsx';

export default function ChatHeader({
  onToggleSidebar,
  bots = [],
  activeBotId = null,
  isWorking = false,
  onSelectBot,
  voiceControls,
  notificationControls,
  developerWorkspace,
  onReviewDeveloperChanges,
}) {
  return (
    <header className="chat-header">
      <div className="chat-header-left">
        <button
          type="button"
          className="menu-btn"
          onClick={onToggleSidebar}
          aria-label="Toggle conversation list"
        >
          <span aria-hidden="true">☰</span>
        </button>
        <img className="header-logo" src="/icons/icon-192.png?v=extension-logo" alt="Kyrex Chat" width="32" height="32" />
      </div>
      <div className="chat-header-right">
        <BotPicker bots={bots} activeBotId={activeBotId} onSelectBot={onSelectBot} isWorking={isWorking} />
        {notificationControls}
        {voiceControls}
      </div>
      {developerWorkspace && <div className="developer-workspace-context" aria-label="Developer workspace">
        <strong title={developerWorkspace.repository || undefined}>{developerWorkspace.repository || 'Original workspace'}</strong>
        {developerWorkspace.branch && <code title={developerWorkspace.branch}>{developerWorkspace.branch}</code>}
        {developerWorkspace.review_available && <button type="button" className="delegated-chat-link"
          onClick={() => onReviewDeveloperChanges()}>Review changes</button>}
      </div>}
    </header>
  );
}

