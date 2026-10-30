import React from 'react';
import { botDisplayName } from '../lib/activeWork.js';

export default function Sidebar({
  conversations,
  activeId,
  onSelect,
  onNew,
  onOpenBot,
  onDelete,
  onSettings,
  onBots,
  onConnections,
  // Registry Bots (id/name/status) so a Bot-bound conversation shows the Bot
  // name as its tab title.
  bots = [],
  // { [conversationId]: oneLine } — the active-work subtitle, already derived
  // from durable task/delegation + live SSE state. Absent ⇒ no line.
  activityLines = {},
  open,
}) {
  // Keep every conversation available, but render one top-level row per Bot.
  // Identity is bot_id, never a display name; ordinary chats remain separate.
  const groups = [], byBot = new Map();
  for (const conversation of conversations) {
    if (!conversation.bot_id) {
      groups.push({ key: `chat:${conversation.conversation_id}`, rows: [conversation] });
      continue;
    }
    let group = byBot.get(conversation.bot_id);
    if (!group) {
      group = { key: `bot:${conversation.bot_id}`, rows: [] };
      groups.push(group); byBot.set(conversation.bot_id, group);
    }
    group.rows.push(conversation);
  }
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

  const select = c => c.bot_id && onOpenBot ? onOpenBot(c.bot_id) : onSelect(c.conversation_id);

  const handleItemKey = (e, c) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      select(c);
    }
  };

  const conversationRow = c => (
    <div key={c.conversation_id}
      className={`conversation-item ${c.conversation_id === activeId ? 'active' : ''}`}
      role="button" tabIndex={0}
      aria-current={c.conversation_id === activeId ? 'true' : undefined}
      onClick={() => select(c)} onKeyDown={e => handleItemKey(e, c)}>
      <span className="conversation-text">
        <span className="conversation-title-row">
          <span className="conversation-title">{conversationTitle(c)}</span>
          <span className="conversation-time">{conversationTime(c)}</span>
        </span>
        <span className="conversation-subtitle" title={conversationMeta(c)}>{conversationMeta(c)}</span>
      </span>
      <button type="button" className="conversation-delete" title="Delete conversation"
        aria-label={`Delete conversation: ${conversationTitle(c)}`}
        onKeyDown={e => e.stopPropagation()}
        onClick={e => { e.stopPropagation(); onDelete(c.conversation_id); }}>×</button>
    </div>
  );

  return (
    <aside className={`sidebar ${open ? 'open' : ''}`} aria-label="Conversations">
      <div className="sidebar-brand">
        <img className="brand-mark" src="/icons/icon-192.png?v=extension-logo" alt="" width="30" height="30" />
        <span className="brand-name">Kyrex Chat</span>
      </div>
      <button type="button" className="new-chat-btn" onClick={onNew}>
        <span className="new-chat-plus" aria-hidden="true">+</span> New Chat
      </button>
      <nav className="conversation-list">
        {conversations.length === 0 ? (
          <div className="conversation-empty">No conversations yet</div>
        ) : (
          groups.map(group => {
            const c = group.rows.find(row => row.conversation_id === activeId) || group.rows[0];
            if (!c.bot_id) return conversationRow(c);
            const activity = group.rows.map(row => activityLines[row.conversation_id]).find(Boolean);
            // One ongoing Bot chat, with active work from any legacy thread.
            const row = activity ? { ...c, latest_update: activity } : c;
            return <div className="conversation-group" key={group.key}>{conversationRow(row)}</div>;
          })
        )}
      </nav>
      <button type="button" className="sidebar-settings" onClick={onBots}>Bots</button>
      <button type="button" className="sidebar-settings" onClick={onConnections}>Connections</button>
      <button type="button" className="sidebar-settings" onClick={onSettings}>Settings</button>
    </aside>
  );
}
