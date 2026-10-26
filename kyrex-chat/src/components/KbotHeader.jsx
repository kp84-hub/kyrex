import React, { useEffect, useState } from 'react';
import BotPicker from './BotPicker.jsx';
import { closeKbot, readKbotAnchor, resizeKbot } from '../lib/kbotSurface.js';

const labels = { idle: 'Here with you', thinking: 'Thinking…', replying: 'Replying…', listening: 'Listening…', speaking: 'Talking…' };

export default function KbotHeader({ phase, botName, conversationId, onHistory, voiceControls, bots = [], activeBotId = null, onSelectBot, botPickerDisabled = false, onResize = resizeKbot }) {
  const [anchor, setAnchor] = useState(() => readKbotAnchor(window.__kyrexKbotAnchor));
  useEffect(() => {
    const update = () => { const next = readKbotAnchor(window.__kyrexKbotAnchor); if (next) setAnchor(next); };
    let opening;
    const reveal = () => {
      const app = document.querySelector('.kbot-app');
      opening?.cancel();
      if (app && !window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches) {
        opening = app.animate?.([{ opacity: 0, transform: 'scale(.96) translateY(3px)' }, { opacity: 1, transform: 'none' }], { duration: 160, easing: 'ease-out' });
      }
      app?.querySelector('.composer-input')?.focus();
    };
    const escape = event => {
      if (event.key === 'Escape' && !event.defaultPrevented && !document.querySelector('[role="dialog"], .sidebar.open')) {
        event.preventDefault(); closeKbot();
      }
    };
    window.addEventListener('kyrex-kbot-anchor', update);
    window.addEventListener('kyrex-kbot-open', reveal);
    document.addEventListener('keydown', escape);
    reveal();
    return () => { opening?.cancel(); window.removeEventListener('kyrex-kbot-anchor', update); window.removeEventListener('kyrex-kbot-open', reveal); document.removeEventListener('keydown', escape); };
  }, []);
  useEffect(() => {
    const app = document.querySelector('.kbot-app');
    if (!app || !anchor) return;
    app.dataset.tail = anchor.side;
    if (anchor.size) app.dataset.size = anchor.size;
    else delete app.dataset.size;
    app.style.setProperty('--kbot-tail-y', `${anchor.y}px`);
  }, [anchor]);
  const fullChat = conversationId ? `/?conversation=${encodeURIComponent(conversationId)}` : '/';
  return <header className="kbot-header">
    <button type="button" className="kbot-icon-btn" onClick={onHistory} aria-label="Kbot conversations and settings" title="Conversations and settings">☰</button>
    <div className="kbot-heading"><strong>Kbot</strong><span role="status"><i className={`kbot-status-dot phase-${phase}`} />{labels[phase] || labels.idle}</span><small title={botName}>{botName}</small></div>
    <BotPicker compact bots={bots} activeBotId={activeBotId} onSelectBot={onSelectBot} disabled={botPickerDisabled} />
    {voiceControls}
    {anchor?.size && <label className="kbot-size-picker" title="Window size">
      <svg width="17" height="17" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><rect x="2.5" y="3" width="15" height="14" rx="2" /><path d="M3 7h14M10 10h4v4M14 10l-5 5" /></svg>
      <select aria-label="Kbot window size" value={anchor.size} onChange={event => onResize(event.target.value)}>
        <option value="compact">Compact</option>
        <option value="medium">Medium</option>
        <option value="full">Full screen</option>
      </select>
    </label>}
    <a className="kbot-icon-btn" href={fullChat} target="_blank" rel="noopener noreferrer" aria-label="Open full Kyrex Chat in browser" title="Open full Chat in browser">↗</a>
    <button type="button" className="kbot-icon-btn" onClick={closeKbot} aria-label="Close Kbot bubble" title="Close bubble">×</button>
  </header>;
}
