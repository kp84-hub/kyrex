import React, { useEffect, useState } from 'react';

async function request(method, path = '') {
  const response = await fetch(`/api/connections/voice${path}`, { method, cache: 'no-store' });
  const value = await response.json();
  if (!response.ok) throw new Error(value.detail || 'Voice connection is unavailable.');
  return value;
}

export default function VoiceConnection() {
  const [paired, setPaired] = useState(false);
  const [code, setCode] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { let alive = true; request('GET').then(v => { if (alive) setPaired(v.paired); }).catch(() => {}); return () => { alive = false; }; }, []);
  const act = async kind => {
    if (busy) return;
    setBusy(true); setError('');
    try {
      const value = await request('POST', kind === 'pair' ? '/connect' : '/disconnect');
      if (kind === 'pair') setCode(value); else { setPaired(false); setCode(null); }
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };
  return <details className="connection-card"><summary>Voice · Android {paired ? '— paired' : ''}</summary>
    <p>Link Kyrex Voice to speak with The Overwatcher. Wake detection stays on your phone; recognized requests enter your saved voice chat. Start and stop listening in the phone app.</p>
    <button className="connection-btn" disabled={busy} onClick={() => act('pair')}>Pair voice phone</button>
    {paired && <button className="connection-btn secondary" disabled={busy} onClick={() => act('disconnect')}>Disconnect voice phone</button>}
    {code && <p>In Kyrex Voice, enter server <strong>{window.location.origin}</strong> and pairing code <strong style={{ overflowWrap: 'anywhere' }}>{code.pairing_code}</strong>. The code expires in 10 minutes.</p>}
    {error && <p role="alert">{error}</p>}
  </details>;
}
