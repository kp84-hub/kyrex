import React, { useEffect, useState } from 'react';
import { accountRequest } from '../lib/api.js';
import ManagedChat from './ManagedChat.jsx';
import { isKbotSurface } from '../lib/kbotSurface.js';
import '../Onboarding.css';

function AccountFrame({ children }) {
  if (!isKbotSurface()) return children;
  return <div className="app kbot-app kbot-account-frame"><div className="main">
    <header className="kbot-account-header"><strong>Kbot</strong>
      <a href="/__companion__/hide" aria-label="Hide Kbot">×</a>
    </header>{children}
  </div></div>;
}

export default function AccountGate({ children }) {
  const [options, setOptions] = useState(null), [account, setAccount] = useState(null);
  const [authenticated, setAuthenticated] = useState(false), [loading, setLoading] = useState(true);
  const [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [email, setEmail] = useState(''), [code, setCode] = useState(''), [challenge, setChallenge] = useState('');
  const [adult, setAdult] = useState(false), [name, setName] = useState('');
  const [timezone, setTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
  const [pair, setPair] = useState(() => new URLSearchParams(window.location.hash.slice(1)).get('pair') || '');
  const [desktop, setDesktop] = useState('');
  const [desktopExpired, setDesktopExpired] = useState(false);
  useEffect(() => {
    if (isKbotSurface()) return;
    const tx = new URLSearchParams(window.location.search).get('desktop');
    try {
      let saved = JSON.parse(window.sessionStorage.getItem('kyrex.desktop.handoff') || 'null');
      if (tx && saved?.tx !== tx) {
        saved = { tx, at: Date.now() };
        window.sessionStorage.setItem('kyrex.desktop.handoff', JSON.stringify(saved));
      }
      if (saved && Date.now() - saved.at < 600000) setDesktop(saved.tx);
      else if (saved) {
        window.sessionStorage.removeItem('kyrex.desktop.handoff'); setDesktopExpired(true);
      }
    } catch { if (tx) setDesktop(tx); }
    if (window.location.hash.includes('pair=')) window.history.replaceState(null, '', window.location.pathname + window.location.search);
  }, []);
  const load = async () => {
    setLoading(true); setError('');
    try {
      const config = await accountRequest('/auth/options');
      setOptions(config);
      if (!config.enabled) return;
      const me = await accountRequest('/api/me');
      setAuthenticated(me.authenticated === true);
      if (me.authenticated) {
        const value = await accountRequest('/api/account');
        if (!value.account) throw new Error('Your account could not be loaded. Try signing in again.');
        setAccount(value.account); setName(value.account.name || '');
        if (value.account.timezone) setTimezone(value.account.timezone);
      }
    } catch (e) { setError(e.message); }
    finally { setLoading(false); }
  };
  useEffect(() => { load(); }, []);
  const act = async fn => {
    if (busy) return;
    setBusy(true); setError('');
    try { await fn(); } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };
  if (loading) return <AccountFrame><main className="onboarding" role="status">Opening Kyrex…</main></AccountFrame>;
  if (options?.enabled === false) return children;
  if (authenticated && account?.complete && !desktop && !desktopExpired) {
    return account.kind === 'managed' ? <AccountFrame><ManagedChat account={account} /></AccountFrame> : children;
  }
  if (isKbotSurface() && !authenticated) return <AccountFrame>
    <main className="onboarding account-welcome" aria-label="Kbot sign-in">
      <h1>Sign in to Kbot</h1>
      <p>Continue in your browser with your existing Kyrex account, then return here.</p>
      <a className="onboarding-secondary kbot-signin" href="/auth/login">Sign in in browser</a>
      {error && <><p className="message-error" role="alert">{error}</p><button className="onboarding-secondary" onClick={load}>Retry</button></>}
    </main>
  </AccountFrame>;
  return <AccountFrame><main className="onboarding account-welcome" aria-label="Kyrex account setup">
    <p className="onboarding-kicker">Your own Kyrex</p>
    <h1>{desktopExpired ? 'Desktop sign-in expired' : authenticated ? account?.complete ? 'Continue on your desktop' : 'Make yourself at home' : 'Welcome to Kyrex'}</h1>
    {desktopExpired && <><p>Start sign-in again from Kbot to open a fresh request in your browser.</p>
      <button className="onboarding-secondary" onClick={() => setDesktopExpired(false)}>Stay in browser</button></>}
    {!authenticated && <>
      <p>Sign in or create your account. Connect your calendar, email, or coding projects whenever you’re ready.</p>
      {options?.signup_mode === 'invite' && <p>This preview is invitation-only. Use the email address or GitHub account you were invited with.</p>}
      <label className="account-checkbox"><input type="checkbox" checked={adult} onChange={e => setAdult(e.target.checked)} /> I’m 18 or older. A parent can set up a managed account for a child.</label>
      <div className="account-signin-options">
        {options?.google && <button disabled={busy || !adult} className="onboarding-secondary" onClick={() => window.location.assign('/auth/google/login?adult=true')}>Continue with Google</button>}
        {options?.github && <a className="onboarding-secondary" href="/auth/login">Continue with GitHub</a>}
      </div>
      {options?.email && <form onSubmit={e => { e.preventDefault(); act(async () => {
        if (challenge) {
          await accountRequest('/auth/email/verify', 'POST', { challenge, code });
          setCode(''); await load();
        } else {
          const value = await accountRequest('/auth/email/start', 'POST', { email, adult });
          setChallenge(value.challenge);
        }
      }); }}>
        {!challenge ? <label>Email address<input required type="email" autoComplete="email" value={email} onChange={e => setEmail(e.target.value)} /></label>
          : <><p role="status">Check your inbox for an eight-digit code.</p><label>Sign-in code<input required inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{8}" maxLength={8} value={code} onChange={e => setCode(e.target.value)} /></label></>}
        <button className="send-btn" disabled={busy || !adult}>{busy ? 'Working…' : challenge ? 'Verify and continue' : 'Continue with email'}</button>
        {challenge && <button type="button" className="onboarding-secondary" disabled={busy} onClick={() => { setChallenge(''); setCode(''); }}>Use another email or request a new code</button>}
      </form>}
      <p>Signing in does not connect your Gmail, Calendar, or repositories.</p>
      {options?.managed && <details open={Boolean(pair)}><summary>Pair a parent-managed device</summary>
        <p>Ask your parent for a one-time pairing code. You don’t need an email or GitHub account.</p>
        <form onSubmit={e => { e.preventDefault(); act(async () => { await accountRequest('/auth/managed/pair', 'POST', { code: pair }); setPair(''); await load(); }); }}>
          <label>Pairing code<input required autoComplete="off" maxLength={100} value={pair} onChange={e => setPair(e.target.value)} /></label>
          <button className="send-btn" disabled={busy || !pair}>Pair this device</button>
        </form></details>}
    </>}
    {authenticated && account && !account.complete && <form onSubmit={e => { e.preventDefault(); act(async () => {
      await accountRequest('/api/account/profile', 'PUT', { name, timezone, adult }); await load();
    }); }}>
      <label>What should we call you?<input required maxLength={80} autoComplete="nickname" value={name} onChange={e => setName(e.target.value)} /></label>
      <label>Timezone<input required value={timezone} maxLength={100} onChange={e => setTimezone(e.target.value)} placeholder="America/New_York" /></label>
      <label className="account-checkbox"><input required type="checkbox" checked={adult} onChange={e => setAdult(e.target.checked)} /> I’m 18 or older.</label>
      <button className="send-btn" disabled={busy}>{busy ? 'Saving…' : 'Continue to setup'}</button>
    </form>}
    {authenticated && account?.complete && desktop && <>
      <p>Continue as {account.name || 'your Kyrex account'}. This signs Kbot into the same account.</p>
      <button className="send-btn" disabled={busy} onClick={() => act(async () => {
        let value;
        try { value = await accountRequest('/api/account/desktop/finish', 'POST', { transaction: desktop }); }
        catch (e) {
          if (/desktop.*(invalid|expired|consumed)|invalid desktop OAuth state/i.test(e.message)) {
            window.sessionStorage.removeItem('kyrex.desktop.handoff'); setDesktop(''); setDesktopExpired(true);
          }
          throw e;
        }
        if (!value.url.startsWith('kyrex-chat://auth/callback?')) throw new Error('Unexpected desktop handoff. Start sign-in again.');
        window.sessionStorage.removeItem('kyrex.desktop.handoff'); setDesktop(''); window.location.assign(value.url);
      })}>Continue to Kbot</button>
      <button className="onboarding-secondary" onClick={() => { window.sessionStorage.removeItem('kyrex.desktop.handoff'); setDesktop(''); }}>Stay in browser</button>
    </>}
    {error && <p className="message-error" role="alert">{error}</p>}
    {!options && <><a className="onboarding-secondary" href="/auth/login">Sign in with GitHub</a><button className="onboarding-secondary" onClick={load}>Retry</button></>}
    {authenticated && <a className="onboarding-secondary" href="/auth/logout">Sign out</a>}
  </main></AccountFrame>;
}
