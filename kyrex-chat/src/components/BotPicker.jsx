import React from 'react';

// Both headers use the same Bot lifecycle rules and conversation navigation.
export default function BotPicker({ bots = [], activeBotId = null, onSelectBot, compact = false, disabled = false }) {
  // Choosing a Bot reopens its latest chat (creating one only if absent).
  // The active conversation's binding is never mutated. A bound Bot that vanished
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
    if (e.target.selectedIndex < 0) return;
    const value = e.target.value || null;
    if (disabled || !onSelectBot) return;
    if (value === activeBotId) return; // unchanged — never a no-op new chat
    if (value && !bots.some((b) => b.id === value && botUsable(b))) return;
    onSelectBot(value);
  };

  const picker = (
    <select
      className={`bot-picker${boundBot ? ' ok' : ''}`}
      disabled={disabled}
      value={activeBotId || ''}
      onChange={handleBotSelect}
      aria-label="Select Bot"
      title={
        boundBot
          ? `This conversation is bound to ${boundBot.name}. Pick another running Bot to open its chat.`
          : 'Pick a running Bot to open its chat (paused/stopped Bots are disabled — start them in Bot settings)'
      }
    >
      <option value="">Kyrex</option>
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
  );
  if (!compact) return picker;
  return <span className={`kbot-bot-picker${boundBot ? ' has-bot' : ''}`}>
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 3v3M10.5 3h3" /><rect x="4" y="6" width="16" height="14" rx="4" />
      <path d="M1.5 11v4M22.5 11v4M9 15.5h6" /><circle cx="8.5" cy="11" r=".8" fill="currentColor" /><circle cx="15.5" cy="11" r=".8" fill="currentColor" />
    </svg>
    {picker}
  </span>;
}
