import React from 'react';

export default function Sidebar({
  conversations,
  activeId,
  onSelect,
  onNew,
  onDelete,
  open,
}) {
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
              <span className="conversation-title">{c.title || 'New chat'}</span>
              <button
                type="button"
                className="conversation-delete"
                title="Delete conversation"
                aria-label={`Delete conversation: ${c.title || 'New chat'}`}
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
    </aside>
  );
}
