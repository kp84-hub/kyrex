import React, { useEffect, useRef, useState } from 'react';
import { VoiceClient } from '../lib/voiceClient.js';

const labels = { stopped: 'Voice off', starting: 'Starting voice…', loading: 'Loading wake detection…', listening: 'Listening for Hey Kyrex', recording: 'Speak after the tone', transcribing: 'Recognizing your request…', working: 'Kyrex is working…', synthesizing: 'Preparing the reply…', speaking: 'Speaking the reply' };
export default function VoiceControls({ onConversation, onResult, disabled = false }) {
  const [state, setState] = useState({ phase: 'stopped' });
  const callbacks = useRef({ onConversation, onResult }); callbacks.current = { onConversation, onResult };
  const client = useRef(null);
  useEffect(() => {
    const voice = new VoiceClient({ onState: update => setState(previous => ({ ...previous, ...update })),
      onConversation: cid => callbacks.current.onConversation?.(cid), onResult: cid => callbacks.current.onResult?.(cid) });
    client.current = voice;
    const leave = () => { if (document.hidden || !document.hasFocus?.()) voice.stop(); };
    const hide = () => voice.stop();
    document.addEventListener('visibilitychange', leave); window.addEventListener('pagehide', hide);
    return () => { document.removeEventListener('visibilitychange', leave); window.removeEventListener('pagehide', hide); voice.onState = () => {}; voice.stop(); };
  }, []);
  useEffect(() => { if (disabled) client.current?.stop(); }, [disabled]);
  const active = state.phase !== 'stopped';
  const start = wake => { setState({ phase: 'starting' }); client.current.start(wake); };
  return <div className="voice-controls" aria-label="Voice with The Overwatcher">
    <button type="button" disabled={disabled || active} onClick={() => start(false)} aria-label="Talk to Kyrex">🎙 Talk</button>
    <button type="button" disabled={disabled || active} onClick={() => start(true)}>Hey Kyrex</button>
    {active && <button type="button" className="voice-stop" onClick={() => client.current.stop()}>Stop voice</button>}
    {state.phase === 'recording' && <button type="button" onClick={() => client.current.finishCapture()}>Done speaking</button>}
    <span role="status">{labels[state.phase]}</span>
    {active && <meter min="0" max="100" value={state.level || 0} aria-label="Microphone activity" />}
    {state.error && <span role="alert" className="voice-error">{state.error}</span>}
  </div>;
}
