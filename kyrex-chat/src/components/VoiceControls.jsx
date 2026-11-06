import React, { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { VoiceClient } from '../lib/voiceClient.js';
import VoiceCore from './VoiceCore.jsx';
import VoiceIcon from './VoiceIcon.jsx';
import VoiceModeButton from './VoiceModeButton.jsx';

const labels = { stopped: 'Voice off', starting: 'Starting voice…', loading: 'Loading wake detection…', listening: 'Listening for Kyrex', resuming: 'Resuming wake listening…', recording: 'Speak after the tone', transcribing: 'Recognizing your request…', working: 'Kyrex is working…', synthesizing: 'Preparing the reply…', speaking: 'Speaking the reply', interrupting: 'Interrupting the reply…', followup_wait: 'Preparing to listen again…', followup: 'Listening for your follow-up'  };
export default function VoiceControls({ onConversation, onResult, onPhaseChange, compact = false, disabled = false }) {
  const [state, setState] = useState({ phase: 'stopped' });
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const container = useRef(null), trigger = useRef(null), panel = useRef(null);
  const callbacks = useRef({ onConversation, onResult, onPhaseChange }); callbacks.current = { onConversation, onResult, onPhaseChange };
  const client = useRef(null);
  const minimize = () => { setOpen(false); trigger.current?.focus(); };
  useEffect(() => {
    const voice = new VoiceClient({ onState: update => { setState(previous => ({ ...previous, ...update })); if (update.phase) callbacks.current.onPhaseChange?.(update.phase); },
      onConversation: cid => callbacks.current.onConversation?.(cid), onResult: cid => callbacks.current.onResult?.(cid) });
    client.current = voice;
    const leave = () => { voice.visibilityChanged(document.hidden); };
    const hide = () => voice.stop();
    document.addEventListener('visibilitychange', leave); window.addEventListener('pagehide', hide);
    return () => { document.removeEventListener('visibilitychange', leave); window.removeEventListener('pagehide', hide); voice.onState = () => {}; voice.stop(); };
  }, []);
  useEffect(() => { if (disabled) client.current?.stop(); }, [disabled]);
  useEffect(() => {
    // The native companion's explicit Talk shortcut enters this same voice
    // session. It never takes over another device or restarts an active turn.
    const talk = () => {
      const requested = window.__kyrexDesktopTalkPending;
      if (typeof requested !== 'number' || Date.now() - requested > 15000) return;
      if (disabled || document.hidden) return;
      window.__kyrexDesktopTalkPending = false;
      setOpen(true);
      if (client.current?.phase === 'stopped') client.current.start(false);
    };
    window.addEventListener('kyrex-desktop-talk', talk);
    document.addEventListener('visibilitychange', talk);
    if (window.__kyrexDesktopTalkPending) talk();
    return () => { window.removeEventListener('kyrex-desktop-talk', talk); document.removeEventListener('visibilitychange', talk); };
  }, [disabled]);
  useEffect(() => {
    if (!open) return undefined;
    (panel.current?.querySelector('.voice-primary:not(:disabled)') || panel.current?.querySelector('button:not(:disabled)'))?.focus();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const close = event => {
      if (event.type === 'keydown') {
        if (event.key === 'Tab') {
          const buttons = [...(panel.current?.querySelectorAll('button:not(:disabled), [href], select, input') || [])];
          const first = buttons[0], last = buttons.at(-1);
          if (event.shiftKey && (document.activeElement === first || !panel.current?.contains(document.activeElement))) { event.preventDefault(); last?.focus(); }
          else if (!event.shiftKey && (document.activeElement === last || !panel.current?.contains(document.activeElement))) { event.preventDefault(); first?.focus(); }
          return;
        }
        if (event.key !== 'Escape') return;
        event.preventDefault();
      }
      if (event.type === 'pointerdown' && (container.current?.contains(event.target) || panel.current?.contains(event.target))) return;
      minimize();
    };
    document.addEventListener('pointerdown', close);
    document.addEventListener('keydown', close);
    return () => { document.body.style.overflow = previousOverflow; document.removeEventListener('pointerdown', close); document.removeEventListener('keydown', close); };
  }, [open]);
  const active = !['stopped', 'elsewhere'].includes(state.phase);
  const start = (wake, takeover = false) => { setState({ phase: 'starting' }); client.current.start(wake, takeover); };
  const status = state.phase === 'elsewhere'
    ? (state.device ? `Voice is listening on ${state.device}.` : 'Voice is listening on another device.')
    : labels[state.phase];
  const level = Math.max(0, Math.min(100, Number(state.level) || 0));
  const interruptible = ['working', 'synthesizing', 'speaking'].includes(state.phase);
  return <div className="voice-controls" ref={container}>
    <button ref={trigger} type="button" className={`voice-trigger${active ? ' is-active' : ''}`}
      disabled={disabled && !active} aria-label={compact && !active ? "Talk to Kbot" : "Voice controls"} aria-haspopup="dialog"
      aria-expanded={open} aria-controls={panelId} title={status}
      onClick={() => { if (compact && state.phase === 'stopped') { setOpen(true); start(false); } else setOpen(value => !value); }}>
      <VoiceIcon width="18" height="18" />
      <span>{active ? 'Voice on' : 'Voice'}</span>
    </button>
    <span role="status" className="sr-only">{status}</span>
    {open && createPortal(<div className="voice-surface" data-compact={compact || undefined}>
      <div className="voice-scrim" aria-hidden="true" />
      <section ref={panel} id={panelId} className="voice-panel" data-phase={state.phase} role="dialog" aria-modal="true" aria-label="Voice options">
        <header className="voice-panel-heading"><span><span className="voice-brand-dot" />Kyrex <span className="voice-wordmark-detail">Voice</span></span>
          <button type="button" className="voice-minimize" aria-label="Minimize voice" title="Back to Chat. Listening stays on." onClick={minimize}><VoiceIcon name="close" /></button>
        </header>
        <div className="voice-experience">
          <div className="voice-orb-space" aria-hidden="true"><VoiceCore phase={state.phase} level={level} /></div>
          <div className="voice-copy">
            <h2 className="voice-status" role="status">{state.phase === 'stopped' && !state.error ? 'Ready' : status}</h2>
            {state.phase === 'elsewhere' && <p className="voice-device-notice">Switching will stop listening on that device.</p>}
          </div>
          {state.error && <p role="alert" className="voice-error">{state.error}</p>}
        </div>
        <div className="voice-actions">
          {state.phase === 'elsewhere' ? <button type="button" className="voice-primary" disabled={disabled} onClick={() => start(client.current.wake, true)}><VoiceIcon name="mic" />Use this browser</button>
            : !active ? <VoiceModeButton disabled={disabled} onSelect={wake => start(wake)} /> : <>
              {state.phase === 'listening' && <VoiceModeButton disabled={disabled} onSelect={wake => { if (!wake) client.current.capture(); }} />}
              {state.phase === 'recording' && <button type="button" className="voice-primary" onClick={() => client.current.finishCapture()}><VoiceIcon name="check" />Done speaking</button>}
              {interruptible && <button type="button" className="voice-primary" onClick={() => client.current.interrupt()}><VoiceIcon name="pause" />Interrupt reply</button>}
              <button type="button" className="voice-stop" onClick={() => client.current.stop()}><VoiceIcon name="stop" />Stop voice</button>
            </>}
        </div>
        {active && <meter className="sr-only" min="0" max="100" value={level} aria-label="Microphone activity" />}
      </section>
    </div>, document.body)}
  </div>;
}

