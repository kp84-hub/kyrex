import React, { useEffect, useState } from 'react';

async function request(method, path = '', data) {
  const response = await fetch(`/api/connections/voice${path}`, { method, cache: 'no-store',
    ...(data ? { body: JSON.stringify(data), headers: { 'Content-Type': 'application/json' } } : {}) });
  const value = await response.json();
  if (!response.ok) throw new Error(value.detail || 'Voice connection is unavailable.');
  return value;
}

export default function VoiceConnection() {
  const [view, setView] = useState({ devices: [] });
  const [code, setCode] = useState(null), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [apiKey, setApiKey] = useState(''), [voice, setVoice] = useState('cedar'), [engine, setEngine] = useState('openai');
  const [saved, setSaved] = useState('');
  const [open, setOpen] = useState(new URLSearchParams(window.location.search).get('settings') === 'voice');
  const refresh = async () => {
    const next = await request('GET'); setView(next); setVoice(next.settings?.voice || 'cedar');
    setEngine(next.settings?.configured ? next.settings.engine : 'openai');
  };
  useEffect(() => {
    let alive = true;
    const load = () => request('GET').then(next => {
      if (alive) { setView(next); setVoice(next.settings?.voice || 'cedar'); setEngine(next.settings?.configured ? next.settings.engine : 'openai'); }
    }).catch(() => {});
    load(); window.addEventListener('focus', load);
    return () => { alive = false; window.removeEventListener('focus', load); };
  }, []);
  const act = async (path, data, paired = false) => {
    if (busy) return;
    setBusy(true); setError(''); setSaved('');
    try {
      const value = await request('POST', path, data);
      if (paired) setCode(value); else { if (path === '/disconnect') setCode(null); await refresh(); }
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };
  const save = async remove => {
    if (busy) return;
    setBusy(true); setError('');
    try {
      await request('POST', '/settings', { engine: remove ? 'device' : engine, voice, ...(apiKey ? { api_key: apiKey.trim() } : {}), remove_key: remove });
      setApiKey(''); await refresh(); setSaved(remove ? 'Speech key removed. Using device voices.' : 'Voice settings saved for all your devices.');
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };
  return <details className="connection-card" open={open} onToggle={event => setOpen(event.currentTarget.open)}><summary>Voice · PC & phone {view.paired ? '— paired' : ''}</summary>
    <p>Use Talk or Hey Kyrex at the top of Chat on this device. Your voice conversation and reply voice are shared across your account.</p>
    <div className="voice-settings">
      <label>Reply voice<select value={engine} onChange={event => setEngine(event.target.value)}>
        <option value="openai">Shared AI voice</option><option value="device">Device voice</option>
      </select></label>
      <label>AI voice<select value={voice} onChange={event => setVoice(event.target.value)}><option value="cedar">Cedar</option><option value="marin">Marin</option></select></label>
      <label>OpenAI API key<input type="password" autoComplete="off" spellCheck={false} value={apiKey}
        onChange={event => setApiKey(event.target.value)} placeholder={view.settings?.configured ? 'Key saved — leave blank to keep it' : 'Enter once for all devices'} /></label>
      <p>AI-generated speech uses your OpenAI API account. The key is encrypted on Kyrex Cloud and never sent back to a device. Browser and Windows command recordings go to OpenAI for transcription; wake audio stays local. Android uses its phone speech service. API usage is billed separately from ChatGPT.</p>
      <button className="connection-btn" disabled={busy} onClick={() => save(false)}>Save voice settings</button>
      {view.settings?.configured && <button className="connection-btn secondary" disabled={busy} onClick={() => save(true)}>Remove speech key</button>}
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
