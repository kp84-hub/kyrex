import React, { useEffect, useState } from 'react';
import { androidHealthIntent, clearAndroidHealthSetup } from '../lib/androidHealth.js';

export default function AndroidHealthSetup({ request }) {
  const [owner, setOwner] = useState('');
  const [ready, setReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);
  useEffect(() => {
    let active = true;
    Promise.all([fetch('/api/me').then(r => r.json()), fetch('/api/android/config').then(r => r.json())])
      .then(([me, config]) => {
        if (!active) return;
        setOwner(me.authenticated ? me.username : '');
        setReady(config.health === true && config.origin === window.location.origin);
      }).catch(() => { if (active) setError('Could not check your account. Reload to try again.'); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);
  async function connect() {
    setBusy(true); setError('');
    try {
      const response = await fetch('/api/android/health/authorize', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ challenge: request.challenge, state: request.state }) });
      if (!response.ok) throw new Error('Health connection could not be authorized. Sign in and start setup again.');
      const value = await response.json();
      if (value.state !== request.state || value.expires_at * 1000 <= Date.now()) throw new Error('Connection expired. Start setup again.');
      androidHealthIntent(value);
      setResult(value);
      clearAndroidHealthSetup(window.sessionStorage);
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  }
  return <main className="android-health-setup provider-settings" aria-label="Connect phone health">
    <h1>Connect Samsung Health</h1>
    <p>Share the Samsung Health readings you choose through Android Health Connect. Kyrex reads this data and never writes health records.</p>
    {loading ? <p role="status">Checking your account…</p> : request.invalid ? <p>Connection link is invalid. Open Health setup again in the Kyrex Android app.</p>
      : !ready ? <p>Android Health setup is not available on this server yet.</p>
      : !owner ? <a className="connection-btn" href="/auth/login">Sign in with GitHub</a>
      : result ? <>
        <p>Return to the app to choose your Android health permissions. This link expires in two minutes.</p>
        <a className="connection-btn" href={androidHealthIntent(result)}>Return to Kyrex</a>
      </> : <>
        <p>Connect this phone to <strong>{owner}</strong>. This replaces the previous phone’s health upload permission and preserves your synced history. Automatic sync stays off until you enable it.</p>
        <button className="connection-btn" disabled={busy || Boolean(error)} onClick={connect}>{busy ? 'Connecting…' : `Connect health to ${owner}`}</button>
      </>}
    {error ? <p role="alert">{error}</p> : null}
    <p><a href="/" onClick={() => clearAndroidHealthSetup(window.sessionStorage)}>Cancel and return to Chat</a></p>
  </main>;
}
