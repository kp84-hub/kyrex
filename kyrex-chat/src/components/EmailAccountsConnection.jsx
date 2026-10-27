import React, { useEffect, useState } from 'react';
import { reserveConsentWindow, consentUrl, navigateConsentWindow, closeConsentWindow } from '../lib/consentWindow.js';

async function request(path = '', data) {
  const response = await fetch('/api/connections/email-inbox' + path, {
    method: data === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
    signal: AbortSignal.timeout(60000),
    ...(data === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.detail || 'Email accounts are unavailable.');
  return value;
}

export default function EmailAccountsConnection({ onChange }) {
  const [view, setView] = useState(null), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false), [pending, setPending] = useState(null);
  const [provider, setProvider] = useState('gmail'), [address, setAddress] = useState(''), [secret, setSecret] = useState('');
  const [host, setHost] = useState('imappro.zoho.com');
  const refresh = async () => { const next = await request('/accounts'); setView(next); return next; };
  useEffect(() => {
    let alive = true;
    const load = () => request('/accounts').then(value => { if (alive) setView(value); })
      .catch(e => { if (alive) setError(e.message); });
    load(); window.addEventListener('focus', load);
    return () => { alive = false; window.removeEventListener('focus', load); };
  }, []);
  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(() => {
      if (Date.now() > pending.deadline) { setPending(null); setNotice('Authorization is still pending; check connection or reconnect the account.'); return; }
      refresh().then(next => {
        if (next.accounts?.some(row => row.id === pending.id && row.status === 'connected'
          && row.connection_revision && row.connection_revision !== pending.revision)) {
          setPending(null); setNotice('Mailbox connected. Choose an email search scope below.'); onChange?.();
        }
      }).catch(e => setError(e.message));
    }, 2500);
    return () => clearInterval(timer);
  }, [pending]);
  const act = async (path, data, oauth = false) => {
    if (busy) return;
    const popup = oauth ? reserveConsentWindow() : null;
    setBusy(true); setError(''); setNotice('');
    try {
      const next = await request(path, data);
      setSecret('');
      if (oauth) {
        const url = consentUrl(next.authorization_url);
        const opened = navigateConsentWindow(popup, url);
        setPending({ id: next.account_id, revision: next.connection_revision, url, opened, deadline: next.expires_at * 1000 });
        await refresh();
      } else {
        setView(next); setNotice(path === '/read-scope' ? 'Email search scope saved.' : 'Email accounts updated.'); onChange?.();
      }
    } catch (e) { closeConsentWindow(popup); setError(e.message); }
    finally { setBusy(false); }
  };
  const rows = view?.accounts || [];
  return <details className="connection-card" aria-label="Email accounts">
    <summary>Email accounts · {rows.filter(row => row.id !== 'legacy' && row.status === 'connected').length} additional connected</summary>
    <p>Keep your dedicated inbox and connect additional Gmail, Zoho Mail or AgentMail accounts. Each mailbox has its own read authorization; adding one does not replace Calendar or your dedicated sender.</p>
    {view && <div className="voice-settings">
      <label>Email search scope<select aria-label="Email search scope" value={view.selection || 'legacy'} disabled={busy}
        onChange={e => act('/read-scope', { selection: e.target.value })}>
        <option value="legacy">Existing email reader</option>
        <option value="all">All connected inboxes</option>
        <option value="none">Pause Chat email searches</option>
        {rows.filter(row => row.id !== 'legacy' && row.status === 'connected').map(row => <option key={row.id} value={row.id}>{row.address}</option>)}
      </select></label>
      <p>New accounts are used when selected here or included in All connected inboxes. Searches identify the source mailbox; combined pages are not sorted across accounts by date. Existing email automations keep their configured inbox. A saved connection is checked again when reading; reconnect it if authorization expires.</p>
      <div className="email-account-list">
        {rows.map(row => <article key={row.id} className="email-account-row">
          <strong>{row.label || row.address}</strong><span>{row.provider} · {row.status}</span>
          {row.id === 'legacy' ? <p>Manage this inbox and its separate sending permission in Email inbox below.</p> : <div className="connection-actions">
            {row.provider === 'gmail' && <button type="button" className="connection-btn secondary" disabled={busy}
              onClick={() => act('/accounts/gmail', { account_id: row.id }, true)}>Reconnect {row.address || 'Gmail'}</button>}
            <button type="button" className="connection-btn secondary" disabled={busy}
              onClick={() => act('/accounts/' + row.id + '/disconnect', {})}>Disconnect {row.address || row.provider}</button>
          </div>}
        </article>)}
      </div>
      <form onSubmit={e => { e.preventDefault(); act(provider === 'gmail' ? '/accounts/gmail' : '/accounts',
        provider === 'gmail' ? {} : provider === 'zoho' ? { provider, email: address.trim(), password: secret, host }
          : { provider, inbox_id: address.trim(), api_key: secret }, provider === 'gmail'); }}>
        <label>Add email account<select aria-label="Add email account" value={provider} disabled={busy}
          onChange={e => { setProvider(e.target.value); setAddress(''); setSecret(''); }}>
          <option value="gmail">Gmail / Google Workspace</option><option value="zoho">Zoho Mail</option><option value="agentmail">AgentMail</option>
        </select></label>
        {provider === 'gmail' ? <p>Google will let you choose an account. Kyrex requests Gmail read-only access, verifies the mailbox address, and keeps its tokens on the server.</p> : <>
          <label>{provider === 'zoho' ? 'Additional Zoho mailbox email' : 'Additional AgentMail inbox ID'}<input required
            type={provider === 'zoho' ? 'email' : 'text'} maxLength={254} value={address} disabled={busy} autoComplete="off" onChange={e => setAddress(e.target.value)} /></label>
          {provider === 'zoho' && <label>Zoho incoming server<select value={host} disabled={busy} onChange={e => setHost(e.target.value)}>
            <option value="imappro.zoho.com">Zoho US · custom domain</option><option value="imap.zoho.com">Zoho US · personal</option>
          </select></label>}
          <label>{provider === 'zoho' ? 'Mailbox app password' : 'Inbox-scoped read-only API key'}<input required type="password" maxLength={512}
            value={secret} disabled={busy} autoComplete="new-password" onChange={e => setSecret(e.target.value)} /></label>
          <p>Additional inboxes are read-only. Zoho searches cover a bounded recent Inbox window; aliases share their underlying mailbox. Use the existing dedicated inbox settings for sending.</p>
        </>}
        <button className="connection-btn" disabled={busy || (provider === 'gmail' && !view.gmail_configured)}>
          {busy ? 'Connecting…' : provider === 'gmail' ? 'Add Gmail account' : 'Add read-only inbox'}
        </button>
        {provider === 'gmail' && !view.gmail_configured && <p>The server’s Google connector configuration is needed before Gmail can connect.</p>}
      </form>
    </div>}
    {pending && <p role="status">Finish authorization with Google, then return here. {!pending.opened && <a href={pending.url} target="_blank" rel="noreferrer">Open Google authorization</a>}</p>}
    {notice && <p role="status">{notice}</p>}
    {error && <p className="message-error" role="alert">{error}</p>}
  </details>;
}
