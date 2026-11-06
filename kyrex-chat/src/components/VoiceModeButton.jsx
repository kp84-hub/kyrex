import React, { useEffect, useId, useRef, useState } from 'react';
import VoiceIcon from './VoiceIcon.jsx';

// A release only selects after a deliberate hold and a clear upward diagonal.
const direction = (gesture, event) => event.clientY - gesture.y <= -32 && Math.abs(event.clientX - gesture.x) >= 28
  ? event.clientX < gesture.x ? 'talk' : 'wake' : null;

export default function VoiceModeButton({ onSelect, disabled = false }) {
  const [expanded, setExpanded] = useState(false), [selected, setSelected] = useState(null);
  const id = useId(), container = useRef(null), launcher = useRef(null), gesture = useRef(null);
  const timer = useRef(null), suppressClick = useRef(false);
  const latest = useRef(null); latest.current = { onSelect, disabled };
  const cancel = () => {
    clearTimeout(timer.current); gesture.current = null;
    setExpanded(false); setSelected(null);
  };
  const choose = wake => { cancel(); launcher.current?.focus(); if (!latest.current.disabled) latest.current.onSelect(wake); };
  const suppress = () => { suppressClick.current = true; };
  useEffect(() => {
    const outside = event => { if (!container.current?.contains(event.target)) cancel(); };
    const hidden = () => { if (document.hidden) cancel(); };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('visibilitychange', hidden);
    window.addEventListener('blur', cancel);
    return () => {
      clearTimeout(timer.current); gesture.current = null;
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('visibilitychange', hidden);
      window.removeEventListener('blur', cancel);
    };
  }, []);
  useEffect(() => { if (disabled) cancel(); }, [disabled]);
  return <div className="voice-mode" ref={container} onKeyDown={event => {
    if (event.key === 'Enter' || event.key === ' ') suppressClick.current = false;
    if (event.key === 'Escape' && (expanded || gesture.current)) {
      event.preventDefault(); event.stopPropagation(); cancel(); launcher.current?.focus();
    } else if (['ArrowLeft', 'ArrowRight', 'ArrowUp'].includes(event.key)) {
      event.preventDefault(); setExpanded(true);
      // Focus after React mounts the options, without stealing touch capture.
      queueMicrotask(() => container.current?.querySelector(event.key === 'ArrowRight' ? '[data-mode="wake"]' : '[data-mode="talk"]')?.focus());
    }
  }}>
    {expanded && <div className="voice-mode-options" id={id} role="group" aria-label="Choose voice mode">
      <button type="button" data-mode="talk" data-selected={selected === 'talk' || undefined} disabled={disabled} aria-label="Talk to Kyrex" onClick={() => choose(false)}><VoiceIcon name="up-left" />Talk now</button>
      <button type="button" data-mode="wake" data-selected={selected === 'wake' || undefined} disabled={disabled} onClick={() => choose(true)}><VoiceIcon name="up-right" />Listen for Kyrex</button>
    </div>}
    <button ref={launcher} type="button" className="voice-primary voice-mode-launcher" disabled={disabled}
      aria-label="Choose voice mode" aria-expanded={expanded} aria-controls={expanded ? id : undefined}
      aria-describedby={`${id}-hint`} onContextMenu={event => event.preventDefault()}
      onPointerDown={event => {
        if (event.button !== 0 || event.isPrimary === false || gesture.current) return;
        suppressClick.current = false;
        gesture.current = { id: event.pointerId, x: event.clientX, y: event.clientY, held: false };
        setSelected(null);
        event.currentTarget.setPointerCapture?.(event.pointerId);
        timer.current = setTimeout(() => {
          if (!gesture.current || latest.current.disabled) return;
          gesture.current.held = true; setExpanded(true);
        }, 240);
      }} onPointerMove={event => {
        const current = gesture.current;
        if (current?.id === event.pointerId && current.held) setSelected(direction(current, event));
      }} onPointerUp={event => {
        const current = gesture.current;
        if (!current || current.id !== event.pointerId) return;
        clearTimeout(timer.current); gesture.current = null;
        if (current.held) {
          suppress();
          const mode = direction(current, event);
          if (mode) choose(mode === 'wake'); else cancel();
        }
      }} onPointerCancel={() => { suppress(); cancel(); }}
      onLostPointerCapture={() => { if (gesture.current) { suppress(); cancel(); } }}
      onClick={() => { if (suppressClick.current) suppressClick.current = false; else setExpanded(value => !value); }}>
      <VoiceIcon width="26" height="26" />
    </button>
    <span id={`${id}-hint`} className="voice-mode-hint">Hold &amp; swipe</span>
  </div>;
}
