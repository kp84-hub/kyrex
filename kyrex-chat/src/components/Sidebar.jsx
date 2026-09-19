import React from 'react';
import { botDisplayName } from '../lib/activeWork.js';

export default function Sidebar({
  conversations,
  activeId,
  onSelect,
  onNew,
  onDelete,
  onSettings,
  onBots,
  open,
}) {
  // Registry Bots (id/name/status) so a Bot-bound conversation shows the Bot
  // name as its tab title.
  bots = [],
  // { [conversationId]: oneLine } — the active-work subtitle, already derived
  // from durable task/delegation + live SSE state. Absent ⇒ no line.
  activityLines = {},
  const handleItemKey = (e, id) => {
  // A Bot-bound conversation is titled by its Bot; ordinary chats keep their
  // stored title. The Bot name is the stable identity of the tab.
  const conversationTitle = (c) => {
    if (c.bot_id) return botDisplayName(bots, c.bot_id) || c.title || 'New chat';
    return c.title || 'New chat';
  };

    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onSelect(id);
    }
  };

  return (
    <aside className={`sidebar ${open ? 'open' : ''}`} aria-label="Conversations">
      <div className="sidebar-brand">
        <span className="brand-mark">K</span>
        <span className="brand-name">Kyrex Chat</span>
      </div>
      <button type="button" className="new-chat-btn" onClick={onNew}>
        <span className="new-chat-plus" aria-hidden="true">+</span> New Chat
      </button>
      <nav className="conversation-list">
        {conversations.length === 0 ? (
          <div className="conversation-empty">No conversations yet</div>
        ) : (
          conversations.map((c) => (
            <div
              key={c.conversation_id}
              className={`conversation-item ${
                c.conversation_id === activeId ? 'active' : ''
              }`}
              role="button"
              tabIndex={0}
              aria-current={c.conversation_id === activeId ? 'true' : undefined}
              onClick={() => onSelect(c.conversation_id)}
              onKeyDown={(e) => handleItemKey(e, c.conversation_id)}
            >
              <span className="conversation-text">
                <span className="conversation-title">{conversationTitle(c)}</span>
                {activityLines[c.conversation_id] ? (
                  <span
                    className="conversation-subtitle"
                    title={activityLines[c.conversation_id]}
                  >
                    {activityLines[c.conversation_id]}
                  </span>
                ) : null}
              </span>
              <button
                type="button"
                className="conversation-delete"
                title="Delete conversation"
                aria-label={`Delete conversation: ${conversationTitle(c)}`}
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete(c.conversation_id);
                }}
              >
                ×
              </button>
            </div>
          ))
        )}
      </nav>
      <button type="button" className="sidebar-settings" onClick={onBots}>Bots</button>
      <button type="button" className="sidebar-settings" onClick={onSettings}>Settings</button>
    </aside>
  );
}
