import React, { useEffect, useRef, useState } from 'react';

const providerNames = { google: 'Google Cloud', groq: 'Groq', openai: 'OpenAI' };
const defaultVoices = { google: 'en-US-Chirp3-HD-Leda', openai: 'cedar' };
const defaultModels = { groq: ['whisper-large-v3', 'whisper-large-v3-turbo'], openai: ['gpt-4o-mini-transcribe'] };
const initialForm = { speech_provider: 'google', transcription_provider: 'groq', engine: 'openai',
  voice: defaultVoices.google, transcription_model: defaultModels.groq[0], speaking_rate: 1, follow_up_seconds: 8 };

async function request(method, path = '', data) {
  const response = await fetch(`/api/connections/voice${path}`, { method, cache: 'no-store', signal: AbortSignal.timeout(15000),
    ...(data ? { body: JSON.stringify(data), headers: { 'Content-Type': 'application/json' } } : {}) });
  const value = await response.json();
  if (!response.ok) throw new Error(value.detail || 'Voice connection is unavailable.');
  return value;
}

export default function VoiceConnection() {
  const [view, setView] = useState({ devices: [] });
  const [code, setCode] = useState(null), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [form, setForm] = useState(initialForm), [credentials, setCredentials] = useState({});
  const [saved, setSaved] = useState('');
  const [open, setOpen] = useState(new URLSearchParams(window.location.search).get('settings') === 'voice');
  const dirty = useRef(false), preview = useRef(null), alive = useRef(true);
  const stopPreview = () => {
    if (!preview.current) return;
    preview.current.audio.pause(); URL.revokeObjectURL(preview.current.url); preview.current = null;
  };
  const apply = next => {
    setView(next);
    const settings = next.settings;
    if (settings && !dirty.current) setForm({ ...initialForm, ...settings,
      engine: settings.saved === false ? 'openai' : settings.engine || 'openai',
      speech_provider: settings.speech_provider || 'openai', transcription_provider: settings.transcription_provider || 'openai' });
  };
  const refresh = async () => { const next = await request('GET'); if (alive.current) apply(next); };
  useEffect(() => {
    alive.current = true;
    const load = () => refresh().catch(e => { if (alive.current) setError(e.message); });
    load(); window.addEventListener('focus', load);
    return () => { alive.current = false; window.removeEventListener('focus', load); stopPreview(); };
  }, []);
  const change = value => { dirty.current = true; setSaved(''); stopPreview(); setForm(previous => ({ ...previous, ...value })); };
  const act = async (path, data, paired = false) => {
    if (busy) return;
    setBusy(true); setError(''); setSaved('');
    try {
      const value = await request('POST', path, data);
      if (paired) setCode(value); else { if (path === '/disconnect') setCode(null); await refresh(); }
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };
  const save = async (remove = []) => {
    if (busy) return;
    setBusy(true); setError(''); setSaved(''); stopPreview();
    try {
      const value = await request('POST', '/settings', {
        engine: form.engine, speech_provider: form.speech_provider, voice: form.voice,
        transcription_provider: form.transcription_provider, transcription_model: form.transcription_model,
        speaking_rate: Number(form.speaking_rate), follow_up_seconds: Number(form.follow_up_seconds),
        credentials: Object.fromEntries(Object.entries(credentials).map(([provider, value]) => [provider, value.trim()])),
        remove_credentials: remove,
      });
      if (!alive.current) return;
      setCredentials({}); dirty.current = false; apply({ ...view, settings: value });
      setSaved(remove.length ? 'Saved credential removed. Restart listening to apply changes.' : 'Voice settings saved for all devices. Restart listening to apply changes.');
    } catch (e) { if (alive.current) setError(e.message); }
    finally { if (alive.current) setBusy(false); }
  };
  const playPreview = async () => {
    if (busy) return;
    setBusy(true); setError(''); stopPreview();
    try {
      const response = await fetch('/api/connections/voice/preview', { method: 'POST', cache: 'no-store', signal: AbortSignal.timeout(90000) });
      if (!response.ok) throw new Error((await response.json()).detail || 'Voice preview is unavailable.');
      const blob = await response.blob();
      if (!alive.current) return;
      const url = URL.createObjectURL(blob), audio = new Audio(url);
      preview.current = { url, audio }; audio.onended = stopPreview;
      audio.onerror = () => { stopPreview(); if (alive.current) setError('The voice preview could not play.'); };
      await audio.play();
    } catch (e) { stopPreview(); if (alive.current) setError(e.message); }
    finally { if (alive.current) setBusy(false); }
  };
  const outputProvider = form.engine === 'device' ? 'device' : form.speech_provider;
  const models = view.settings?.options?.transcription_models?.[form.transcription_provider] || defaultModels[form.transcription_provider];
  const voices = view.settings?.options?.voices?.[form.speech_provider] || (form.speech_provider === 'google' ? [defaultVoices.google] : ['cedar', 'marin']);
  const neededCredentials = [...new Set([...(outputProvider === 'device' ? [] : [outputProvider]), form.transcription_provider])];
  return <details className="connection-card" open={open} onToggle={event => setOpen(event.currentTarget.open)}><summary>Voice · PC & phone {view.paired ? '— paired' : ''}</summary>
    <p>Use Talk or Kyrex at the top of Chat on this device. Your voice conversation and reply voice are shared across your account.</p>
    <div className="voice-settings">
      <label>Reply voice provider<select disabled={busy} value={outputProvider} onChange={event => {
        const provider = event.target.value;
        change(provider === 'device' ? { engine: 'device' } : { engine: 'openai', speech_provider: provider,
          voice: provider === form.speech_provider ? form.voice : defaultVoices[provider] });
      }}><option value="google">Google Cloud · Chirp 3 HD</option><option value="openai">OpenAI</option><option value="device">Device voice</option></select></label>
      {outputProvider !== 'device' && <>
        <label>Reply voice<select disabled={busy} value={form.voice} onChange={event => change({ voice: event.target.value })}>
          {voices.map(voice => <option key={voice} value={voice}>{voice.replace('en-US-Chirp3-HD-', '').replace(/^./, letter => letter.toUpperCase())}</option>)}
        </select></label>
        <label>Speaking speed<select disabled={busy} value={form.speaking_rate} onChange={event => change({ speaking_rate: Number(event.target.value) })}>
          {[0.5, 0.75, 1, 1.25, 1.5, 2].map(rate => <option key={rate} value={rate}>{rate === 1 ? 'Normal' : `${rate}×`}</option>)}
        </select></label>
      </>}
      <label>Follow-up listening<select disabled={busy} value={form.follow_up_seconds} onChange={event => change({ follow_up_seconds: Number(event.target.value) })}>
        <option value="0">Off</option><option value="8">8 seconds</option><option value="15">15 seconds</option>
      </select></label>
      <p>After a spoken reply, wait for the beep and continue without a wake phrase. Silence returns to wake listening or ends a Talk session. Stop voice ends listening immediately.</p>
      <label>PC transcription provider<select disabled={busy} value={form.transcription_provider} onChange={event => change({ transcription_provider: event.target.value,
        transcription_model: defaultModels[event.target.value][0] })}>
        <option value="groq">Groq · Whisper</option><option value="openai">OpenAI</option>
      </select></label>
      <label>Transcription model<select disabled={busy} value={form.transcription_model} onChange={event => change({ transcription_model: event.target.value })}>
        {models.map(model => <option value={model} key={model}>{model}</option>)}
      </select></label>
      {neededCredentials.map(provider => <label key={provider}>{provider === 'google' ? 'Google Cloud service account JSON' : `${providerNames[provider]} API key`}
        {provider === 'google' ? <textarea disabled={busy} rows={4} maxLength={16000} autoComplete="off" spellCheck={false} value={credentials.google || ''}
          onChange={event => { dirty.current = true; setCredentials(previous => ({ ...previous, google: event.target.value })); }}
          placeholder={view.settings?.credentials?.google ? 'Credentials saved — leave blank to keep them' : 'Paste your Text-to-Speech service account JSON once'} />
          : <input disabled={busy} type="password" autoComplete="off" spellCheck={false} value={credentials[provider] || ''}
            onChange={event => { dirty.current = true; setCredentials(previous => ({ ...previous, [provider]: event.target.value })); }}
            placeholder={view.settings?.credentials?.[provider] ? 'Key saved — leave blank to keep it' : 'Enter once for all devices'} />}
      </label>)}
      {outputProvider === 'google' && <p>Google Cloud creates AI-generated replies. Enable Cloud Text-to-Speech in your Google project and use a service account with access. <a href="https://docs.cloud.google.com/text-to-speech/docs/authentication" target="_blank" rel="noreferrer">Google setup</a></p>}
      <p>Browser and Windows command recordings go to {providerNames[form.transcription_provider]} for transcription. Wake audio stays local. Android uses its phone speech service. {outputProvider !== 'device' ? `Reply text goes to ${providerNames[outputProvider]} for AI-generated speech.` : 'Replies use each device’s own voice.'} Speech provider usage and billing are separate from your chat model or subscription.</p>
      <p>Credentials are encrypted on Kyrex Cloud, shared only within your account, and never returned to devices. Blank fields keep saved credentials.</p>
      {view.settings && !view.settings.transcription_configured && <p>PC transcription still needs its provider key.</p>}
      {view.settings && !view.settings.speech_configured && <p>The saved reply voice still needs its provider credentials.</p>}
      <button className="connection-btn" disabled={busy} onClick={() => save()}>Save voice settings</button>
      <button className="connection-btn secondary" disabled={busy || dirty.current || !view.settings?.speech_configured || view.settings?.engine === 'device'} onClick={playPreview}>Preview saved voice</button>
      {Object.entries(view.settings?.credentials || {}).filter(([, present]) => present).map(([provider]) => <button key={provider} className="connection-btn secondary" disabled={busy} onClick={() => save([provider])}>Remove {providerNames[provider]} credentials</button>)}
    </div>
    {view.active_device && <p>Listening on: {view.active_device.name}. Starting voice on another device switches listening.</p>}
    <p>For listening with the phone locked or browser closed, pair the Android or Windows companion.</p>
    <button className="connection-btn" disabled={busy} onClick={() => act('/connect', { platform: 'android', name: 'Android phone' }, true)}>Pair voice phone</button>
    <button className="connection-btn" disabled={busy} onClick={() => act('/connect', { platform: 'windows', name: 'Windows PC' }, true)}>Pair Windows PC</button>
    {(view.paired || code) && <button className="connection-btn secondary" disabled={busy} onClick={() => act('/disconnect')}>Disconnect all voice devices</button>}
    {(view.devices || []).map(device => <div className="voice-device" key={device.id}><span>{device.name}</span>
      <button className="connection-btn secondary" disabled={busy} onClick={() => act(`/devices/${device.id}/disconnect`)}>Disconnect</button></div>)}
    {code && <p>In Kyrex Voice, enter server <strong>{window.location.origin}</strong> and pairing code <strong style={{ overflowWrap: 'anywhere' }}>{code.pairing_code}</strong>. The code expires in 10 minutes.</p>}
    {code && <button className="connection-btn secondary" onClick={async () => {
      try { await navigator.clipboard.writeText(code.pairing_code); }
      catch { setError('Select and copy the voice pairing code above.'); }
    }}>Copy voice pairing code</button>}
    {saved && <p role="status">{saved}</p>}{error && <p role="alert">{error}</p>}
  </details>;
}
