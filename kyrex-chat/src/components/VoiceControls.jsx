import React, { useEffect, useId, useRef, useState } from 'react';
import { VoiceClient } from '../lib/voiceClient.js';

const labels = { stopped: 'Voice off', starting: 'Starting voice…', loading: 'Loading wake detection…', listening: 'Listening for Kyrex', recording: 'Speak after the tone', transcribing: 'Recognizing your request…', working: 'Kyrex is working…', synthesizing: 'Preparing the reply…', speaking: 'Speaking the reply', interrupting: 'Interrupting the reply…', followup_wait: 'Preparing to listen again…', followup: 'Listening for your follow-up'  };
export default function VoiceControls({ onConversation, onResult, disabled = false }) {
  const [state, setState] = useState({ phase: 'stopped' });
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const container = useRef(null), trigger = useRef(null), panel = useRef(null);
  const callbacks = useRef({ onConversation, onResult }); callbacks.current = { onConversation, onResult };
  const client = useRef(null);
  useEffect(() => {
    const voice = new VoiceClient({ onState: update => setState(previous => ({ ...previous, ...update })),
      onConversation: cid => callbacks.current.onConversation?.(cid), onResult: cid => callbacks.current.onResult?.(cid) });
    client.current = voice;
    const leave = () => { if (document.hidden) voice.stop(); };
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
    panel.current?.querySelector('button:not(:disabled)')?.focus();
    const close = event => {
      if (event.type === 'keydown' && event.key !== 'Escape') return;
      if (event.type === 'pointerdown' && container.current?.contains(event.target)) return;
      setOpen(false);
      if (event.type === 'keydown') { event.preventDefault(); trigger.current?.focus(); }
    };
    document.addEventListener('pointerdown', close);
    document.addEventListener('keydown', close);
    return () => { document.removeEventListener('pointerdown', close); document.removeEventListener('keydown', close); };
  }, [open]);
  const active = !['stopped', 'elsewhere'].includes(state.phase);
  const start = (wake, takeover = false) => { setState({ phase: 'starting' }); client.current.start(wake, takeover); };
  const status = state.phase === 'elsewhere'
    ? (state.device ? `Voice is listening on ${state.device}.` : 'Voice is listening on another device.')
    : labels[state.phase];
  return <div className="voice-controls" ref={container}>
    <button ref={trigger} type="button" className={`voice-trigger${active ? ' is-active' : ''}`}
      disabled={disabled && !active} aria-label="Voice controls" aria-haspopup="dialog"
      aria-expanded={open} aria-controls={panelId} title={status}
      onClick={() => setOpen(value => !value)}>
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
        <rect x="9" y="2" width="6" height="12" rx="3" /><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8" />
      </svg>
      <span>{active ? 'Voice on' : 'Voice'}</span>
    </button>
    <span role="status" className="sr-only">{status}</span>
    {open && <div ref={panel} id={panelId} className="voice-panel" role="dialog" aria-label="Voice options">
      <div className="voice-panel-heading">Voice</div>
      <p className="voice-status" aria-hidden="true">{status}</p>
      {!active && <>
        <button type="button" disabled={disabled} onClick={() => start(false)} aria-label="Talk to Kyrex">Talk now</button>
        <button type="button" disabled={disabled} onClick={() => start(true)}>Listen for Kyrex</button>
      </>}
      {active && <button type="button" className="voice-stop" onClick={() => client.current.stop()}>Stop voice</button>}
      {['working', 'synthesizing', 'speaking'].includes(state.phase) && <>
        <button type="button" onClick={() => client.current.interrupt()}>Interrupt reply</button>
        {client.current?.wake && <p>Say “Kyrex” to interrupt. Wait for the tone before your next request.</p>}
      </>}
      {state.phase === 'recording' && <button type="button" onClick={() => client.current.finishCapture()}>Done speaking</button>}
      {state.phase === 'elsewhere' && <>
        <p>The companion keeps listening when Chat closes. Switching will stop its session.</p>
        <button type="button" disabled={disabled} onClick={() => start(client.current.wake, true)}>Use this browser</button>
      </>}
      {active && <meter min="0" max="100" value={state.level || 0} aria-label="Microphone activity" />}
      {state.error && <span role="alert" className="voice-error">{state.error}</span>}
    </div>}
  </div>;
}

