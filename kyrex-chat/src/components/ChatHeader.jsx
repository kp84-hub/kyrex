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
  bots = [],
  activeBotId = null,
  onSelectBot,
  providers = [],
  activeProvider = null,
  activeModel = null,
  onChangeProvider,
}) {
  const attached = workspaces.find((w) => w.id === activeWorkspaceId);

  const handleSelect = (e) => {
    const value = e.target.value || null;
    if (onAttachWorkspace) onAttachWorkspace(value);
  };

  // The picker is a "start a conversation with this Bot" control, NOT a
  // rebind control: the active conversation's binding is shown but never
  // mutated by this select. Choosing a different Bot starts a new
  // Bot-bound conversation (server-validated). A bound Bot that vanished
  // server-side is still shown (as unavailable) so the current conversation
  // stays visibly attributed to it.
  const boundBot =
    bots.find((b) => b.id === activeBotId) ||
    (activeBotId ? { id: activeBotId, name: `${activeBotId} (unavailable)` } : null);

  const handleBotSelect = (e) => {
    const value = e.target.value || null;
    if (!onSelectBot) return;
    if (value === activeBotId) return; // unchanged — never a no-op new chat
    onSelectBot(value);
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
        {/* Bot picker — a "start a conversation with this Bot" control. The
            controlled value shows the ACTIVE conversation's binding; picking
            a different Bot starts a new Bot-bound conversation and never
            mutates the binding of the current one (use the sidebar "New
            Chat" for an ordinary, Bot-free conversation). */}
        <select
          className={`status-pill bot-picker${boundBot ? ' ok' : ''}`}
          value={activeBotId || ''}
          onChange={handleBotSelect}
          aria-label="Select Bot"
          title={
            boundBot
              ? `This conversation is bound to ${boundBot.name}. Pick another Bot to start a new conversation with it.`
              : 'Pick a Bot to start a Bot-bound conversation'
          }
        >
          <option value="">No bot</option>
          {bots.map((b) => (
            <option key={b.id} value={b.id}>
              {b.name || b.id}
              {b.status && b.status !== 'running' ? ` (${b.status})` : ''}
            </option>
          ))}
          {boundBot &&
            !bots.some((b) => b.id === activeBotId) && (
              <option key={boundBot.id} value={boundBot.id}>
                {boundBot.name}
              </option>
            )}
        </select>
        <select
          className="status-pill provider-picker"
          value={activeProvider || (providers[0] && providers[0].id) || ''}
          disabled={Boolean(activeBotId) || !activeProvider || !onChangeProvider}
          onChange={(e) => {
            const p = providers.find((item) => item.id === e.target.value);
            if (p && p.models.length) onChangeProvider(p.id, p.models.includes(activeModel) ? activeModel : p.models[0]);
          }}
          aria-label="Select provider"
          title={activeBotId ? "Bot conversations use the Bot's configured provider" : "Provider for this conversation"}
        >
          {providers.map((p) => <option key={p.id} value={p.id}>{p.label || p.id}</option>)}
        </select>
        <select
          className="status-pill model-picker"
          value={activeModel || ''}
          disabled={Boolean(activeBotId) || !activeProvider || !onChangeProvider}
          onChange={(e) => onChangeProvider(activeProvider, e.target.value)}
          aria-label="Select model"
          title={activeBotId ? "Bot conversations use the Bot's configured model" : "Model for this conversation"}
        >
          {(providers.find((p) => p.id === activeProvider)?.models || []).map((model) =>
            <option key={model} value={model}>{model}</option>
          )}
        </select>
        {/* Always a controlled select: when a workspace is attached it shows
            as the selected option, and picking "No workspace" detaches it.
            (Previously the connected state rendered as a static pill with no
            way to unselect the repo.) */}
        <select
          className={`status-pill workspace-picker${attached ? ' ok' : ''}`}
          value={activeWorkspaceId || ''}
          onChange={handleSelect}
          aria-label="Attach workspace"
          disabled={Boolean(boundBot)}
          title={
            boundBot
              ? 'A Bot-bound conversation uses the Bot’s Rift — workspaces cannot be attached.'
              : attached
                ? 'A repo/workspace is attached — Kyrex can inspect it (read-only). Select "No workspace" to detach.'
                : 'Attach a server-registered workspace (read-only inspection)'
          }
        >
          <option value="">No workspace</option>
          {workspaces.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name}
              {w.available === false ? ' (unavailable)' : ''}
            </option>
          ))}
        </select>
      </div>
    </header>
  );
}
