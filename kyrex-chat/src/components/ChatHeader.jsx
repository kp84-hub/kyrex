import React from 'react';

export default function ChatHeader({
  onToggleSidebar,
  bots = [],
  activeBotId = null,
  onSelectBot,
}) {
  // The picker is a "start a conversation with this Bot" control, NOT a
  // rebind control: the active conversation's binding is shown but never
  // mutated by this select. Choosing a different Bot starts a new
  // Bot-bound conversation (server-validated). A bound Bot that vanished
  // server-side is still shown (as unavailable) so the current conversation
  // stays visibly attributed to it.
  const boundBot =
    bots.find((b) => b.id === activeBotId) ||
    (activeBotId ? { id: activeBotId, name: `${activeBotId} (unavailable)` } : null);

  // A Bot is selectable only when it is lifecycle-running AND its Rift
  // resolves. Unusable Bots stay VISIBLE but DISABLED, with the reason in the
  // label — selecting a Bot never silently starts it (and the server rejects
  // a new binding to a paused/stopped Bot regardless of the UI).
  const botUsable = (b) => b.available !== false && b.status === 'running';
  const botReason = (b) => {
    if (b.available === false) return 'rift unavailable';
    if (b.status && b.status !== 'running') {
      return `${b.status} — start it in Bot settings`;
    }
    return '';
  };

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
        {/* Bot picker — a "start a conversation with this Bot" control. The
            controlled value shows the ACTIVE conversation's binding; picking
            a different Bot starts a new Bot-bound conversation and never
            mutates the binding of the current one (use the sidebar "New
            Chat" for an ordinary, Bot-free conversation). */}
        <select
          className={`bot-picker${boundBot ? ' ok' : ''}`}
          value={activeBotId || ''}
          onChange={handleBotSelect}
          aria-label="Select Bot"
          title={
            boundBot
              ? `This conversation is bound to ${boundBot.name}. Pick another running Bot to start a new conversation with it.`
              : 'Pick a running Bot to start a Bot-bound conversation (paused/stopped Bots are disabled — start them in Bot settings)'
          }
        >
          <option value="">No bot</option>
          {bots.map((b) => {
            const usable = botUsable(b);
            const why = usable ? '' : botReason(b);
            return (
              <option
                key={b.id}
                value={b.id}
                disabled={!usable}
                title={usable ? undefined : `Unavailable: ${why}`}
              >
                {b.name || b.id}
                {why ? ` (${why})` : ''}
              </option>
            );
          })}
          {boundBot &&
            !bots.some((b) => b.id === activeBotId) && (
              <option key={boundBot.id} value={boundBot.id}>
                {boundBot.name}
              </option>
            )}
        </select>
      </div>
    </header>
  );
}
