import React from 'react';
import BotPicker from './BotPicker.jsx';

export default function ChatHeader({
  onToggleSidebar,
  bots = [],
  activeBotId = null,
  onSelectBot,
  voiceControls,
  notificationControls,
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
        <BotPicker bots={bots} activeBotId={activeBotId} onSelectBot={onSelectBot} />
        {notificationControls}
        {voiceControls}
      </div>
    </header>
  );
}

