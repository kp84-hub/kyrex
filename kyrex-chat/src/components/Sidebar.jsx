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
  // Registry Bots (id/name/status) so a Bot-bound conversation shows the Bot
  // name as its tab title.
  bots = [],
  // { [conversationId]: oneLine } — the active-work subtitle, already derived
  // from durable task/delegation + live SSE state. Absent ⇒ no line.
  activityLines = {},
  open,
}) {
  const botForConversation = (c) => bots.find((bot) => bot.id === c.bot_id);

  // A Bot-bound conversation is titled by its Bot; ordinary chats keep their
  // stored title. The Bot name is the stable identity of the tab.
  const conversationTitle = (c) => {
    if (c.bot_id) return botDisplayName(bots, c.bot_id) || c.title || 'New chat';
    return c.title || 'New chat';
  };

  const conversationMeta = (c) => {
    const bot = botForConversation(c);
    if (activityLines[c.conversation_id]) return activityLines[c.conversation_id];
    if (c.latest_update) return c.latest_update;
    if (bot) return bot.status === 'running' ? 'Ready' : (bot.status || 'Stopped');
    return 'Kyrex chat';
  };

  const conversationTime = (c) => {
    if (!c.updated_at) return '';
    const value = new Date(c.updated_at);
    if (Number.isNaN(value.getTime())) return '';
    const now = new Date();
    if (value.toDateString() === now.toDateString()) {
      return value.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    }
    return value.toLocaleDateString([], { month: 'short', day: 'numeric' });
  };

  const handleItemKey = (e, id) => {
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
                <span className="conversation-title-row">
                  <span className="conversation-title">{conversationTitle(c)}</span>
                  <span className="conversation-time">{conversationTime(c)}</span>
                </span>
                <span
                  className="conversation-subtitle"
                  title={conversationMeta(c)}
                >
                  {conversationMeta(c)}
                </span>
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
