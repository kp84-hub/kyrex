import React, { useEffect, useState } from 'react';
import { androidNativeIntent, clearAndroidHealthSetup } from '../lib/androidHealth.js';

export default function AndroidHealthSetup({ request }) {
  const capability = request.capability || 'health';
  const label = { health: 'Samsung Health', voice: 'Voice', messages: 'Messages' }[capability] || 'Phone';
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
        setReady(config[capability] === true && config.origin === window.location.origin);
      }).catch(() => { if (active) setError('Could not check your account. Reload to try again.'); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [capability]);
  async function connect() {
    setBusy(true); setError('');
    try {
      const response = await fetch(`/api/android/${capability === 'health' ? 'health' : 'native'}/authorize`, { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ challenge: request.challenge, state: request.state, expected_owner: owner,
          ...(capability === 'health' ? {} : { capability }) }) });
      if (!response.ok) throw new Error('Phone connection could not be authorized. Sign in and start setup again.');
      const value = await response.json();
      if (value.state !== request.state || value.expires_at * 1000 <= Date.now()) throw new Error('Connection expired. Start setup again.');
      androidNativeIntent(capability, value);
      setResult(value);
      clearAndroidHealthSetup(window.sessionStorage);
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  }
  return <main className="android-health-setup provider-settings" aria-label={`Connect phone ${label}`}>
    <h1>Connect {label}</h1>
    <p>{capability === 'health' ? 'Share the Samsung Health readings you choose through Android Health Connect. Kyrex reads this data and never writes health records.'
      : capability === 'voice' ? 'Use your existing Kyrex voice conversation and speech settings. Wake listening starts only when you enable it on this phone; you can stop it from its notification.'
      : 'Link the Google Messages connection on this phone to Kyrex. Sync shares up to 100 texts from 10 recent conversations. Google session keys stay on the phone. Chat sending stays off until you enable it separately, and each send requires review and confirmation.'}</p>
    {loading ? <p role="status">Checking your account…</p> : request.invalid ? <p>Connection link is invalid. Open Health setup again in the Kyrex Android app.</p>
      : !ready ? <p>Android Health setup is not available on this server yet.</p>
      : !owner ? <a className="connection-btn" href="/auth/login">Sign in with GitHub</a>
      : result ? <>
        <p>Return to Kyrex to finish {label} setup. This link expires in two minutes.</p>
        <a className="connection-btn" href={androidNativeIntent(capability, result)}>Return to Kyrex</a>
      </> : <>
        <p>Connect this phone to <strong>{owner}</strong>. {capability === 'health' ? 'This replaces the previous phone’s health upload permission and preserves your synced history. Automatic sync stays off until you enable it.'
          : capability === 'messages' ? 'This replaces the previous Messages upload permission and preserves saved texts and send receipts. Complete any confirmed pending send before switching. Background connection and sending require separate consent on this phone.'
          : 'Microphone access and background listening require separate consent on this phone. Starting listening takes over the existing voice lease.'}</p>
        <button className="connection-btn" disabled={busy || Boolean(error)} onClick={connect}>{busy ? 'Connecting…' : `Connect ${capability} to ${owner}`}</button>
      </>}
    {error ? <p role="alert">{error}</p> : null}
    <p><a href="/" onClick={() => clearAndroidHealthSetup(window.sessionStorage)}>Cancel and return to Chat</a></p>
  </main>;
}
