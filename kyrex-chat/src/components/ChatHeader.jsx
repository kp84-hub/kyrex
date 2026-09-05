import React from 'react';

export default function ChatHeader({ status, onToggleSidebar }) {
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
        <div className="chat-header-title">
          Kyrex Chat
          <span className="chat-header-sub">conversational assistant</span>
        </div>
      </div>
      <div className={`status-pill ${status.available ? 'ok' : 'warn'}`}>
        {status.available ? 'Engine ready' : status.detail || 'Engine unavailable'}
      </div>
    </header>
  );
}
