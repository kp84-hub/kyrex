import React from 'react';

// Two distinct status indicators — they mean different things:
//   * "Provider ready"      → the LLM provider is configured (env keys set).
//   * "Workspace connected" → a registered repo/workspace is actually
//                             attached to the active conversation.
// The provider-only state must never be labelled "Engine ready".
export default function ChatHeader({
  status,
  workspaces = [],
  activeWorkspaceId = null,
  onAttachWorkspace,
  onToggleSidebar,
}) {
  const attached = workspaces.find((w) => w.id === activeWorkspaceId);

  const handleSelect = (e) => {
    const value = e.target.value || null;
    if (onAttachWorkspace) onAttachWorkspace(value);
  };

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
          <span className="chat-header-title-text">Kyrex Chat</span>
          <span className="chat-header-sub">Conversational assistant</span>
        </div>
      </div>
      <div className="chat-header-right">
        <div className={`status-pill ${status.available ? 'ok' : 'warn'}`}>
          {status.available ? 'Provider ready' : status.detail || 'Provider unconfigured'}
        </div>
        {attached ? (
          <div
            className="status-pill ok pill-workspace"
            title={
              attached.available === false
                ? 'Registered workspace is currently unavailable on the server'
                : 'A repo/workspace is attached — Kyrex can inspect it (read-only)'
            }
          >
            Workspace connected: {attached.name}
          </div>
        ) : (
          <select
            className="status-pill workspace-picker"
            value=""
            onChange={handleSelect}
            aria-label="Attach workspace"
            title="Attach a server-registered workspace (read-only inspection)"
          >
            <option value="">No workspace</option>
            {workspaces.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
                {w.available === false ? ' (unavailable)' : ''}
              </option>
            ))}
          </select>
        )}
      </div>
    </header>
  );
}
