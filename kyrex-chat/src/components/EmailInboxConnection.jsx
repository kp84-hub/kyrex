import React, { useEffect, useRef, useState } from 'react';

async function request(path = '', data) {
  const response = await fetch(`/api/connections/email-inbox${path}`, {
    method: data ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
    signal: AbortSignal.timeout(60000),
    ...(data ? { body: JSON.stringify(data), headers: { 'Content-Type': 'application/json' } } : {}),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.detail || 'Email inbox is unavailable.');
  return value;
}

export default function EmailInboxConnection({ onChange }) {
  const [view, setView] = useState(null), [inbox, setInbox] = useState(''), [key, setKey] = useState('');
  const [provider, setProvider] = useState('zoho'), [host, setHost] = useState('imappro.zoho.com');
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [saved, setSaved] = useState('');
  const alive = useRef(true), edited = useRef(false);
  const apply = value => {
    if (!alive.current) return;
    setView(value);
    if (!edited.current) {
      setProvider(value.inbox_id ? value.provider : 'zoho');
      setInbox(value.inbox_id || ''); setHost(value.host || 'imappro.zoho.com');
    }
  };
  useEffect(() => {
    alive.current = true;
    const load = () => request().then(apply).catch(e => { if (alive.current) setError(e.message); });
    load(); window.addEventListener('focus', load);
    return () => { alive.current = false; window.removeEventListener('focus', load); };
  }, []);
  const act = async (path, data) => {
    if (busy) return;
    setBusy(true); setError(''); setSaved('');
    try {
      const next = await request(path, data);
      if (!alive.current) return;
      setKey(''); edited.current = false; apply(next);
      setSaved(path === '/connect' ? 'Dedicated inbox connected. Personal Gmail reads are blocked.'
        : next.source === 'gmail' ? 'Personal Gmail selected. Connect its read permission below if needed.'
          : 'Email reading is disabled. Personal Gmail remains blocked.');
      onChange?.();
    } catch (e) { if (alive.current) setError(e.message); }
    finally { if (alive.current) setBusy(false); }
  };
  const keepSecret = view?.connected && view.provider === provider && inbox.trim() === view.inbox_id
    && (provider !== 'zoho' || host === view.host);
  return <details className="connection-card" aria-label="Email inbox settings">
    <summary>Email inbox · {view?.active ? view.address : view?.source === 'gmail' ? 'Personal Gmail selected' : view ? 'Reading disabled' : 'Loading…'}</summary>
    <p>Give Kyrex its own inbox and forward only the school and other emails you want it to read.</p>
    <p>Connecting a dedicated inbox blocks personal Gmail reads in Kyrex and keeps your Calendar connection. If the selected inbox fails or is disconnected, Kyrex will not fall back to another inbox.</p>
    <div className="voice-settings">
      <label>Inbox provider<select value={provider} disabled={busy} onChange={e => {
        edited.current = true; setProvider(e.target.value); setKey(''); setError(''); setSaved('');
        setInbox(e.target.value === view?.provider ? view.inbox_id : ''); setHost(view?.host || 'imappro.zoho.com');
      }}><option value="zoho">Zoho Mail</option><option value="agentmail">AgentMail</option></select></label>
      {provider === 'zoho' ? <>
        <p>Create a separate mailbox account in <a href="https://mailadmin.zoho.com" target="_blank" rel="noreferrer">Zoho Mail Admin</a> for the address you want Kyrex to use. An alias shares its account’s inbox.</p>
        <p>Enable IMAP in Zoho Mail → Settings → Mail Accounts. Your plan must support IMAP. Generate an app password for this dedicated mailbox account in Zoho Accounts → Security → App passwords.</p>
        <label>Zoho mailbox email<input type="email" value={inbox} maxLength={254} autoComplete="off" disabled={busy}
          onChange={e => { edited.current = true; setInbox(e.target.value); }} placeholder="Your dedicated mailbox address" /></label>
        <label>Zoho incoming server<select value={host} disabled={busy} onChange={e => { edited.current = true; setHost(e.target.value); }}>
          <option value="imappro.zoho.com">Zoho US · custom-domain mailbox</option>
          <option value="imap.zoho.com">Zoho US · personal or legacy mailbox</option>
        </select></label>
        <label>Zoho app password<input type="password" value={key} maxLength={512} autoComplete="new-password" disabled={busy}
          onChange={e => setKey(e.target.value)} placeholder={keepSecret ? 'Leave blank to keep the saved app password' : 'App password for the dedicated mailbox'} /></label>
        <p>Kyrex reads the Inbox without marking messages read. Searches cover a bounded window of recent Inbox messages. Other folders and attachments are not read.</p>
      </> : <>
        <p>Create a dedicated inbox in <a href="https://console.agentmail.to" target="_blank" rel="noreferrer">AgentMail Console</a>. Use an API key scoped to that inbox with only <code>inbox_read</code> and <code>message_read</code> permissions.</p>
        <label>AgentMail inbox ID<input value={inbox} maxLength={254} autoComplete="off" disabled={busy}
          onChange={e => { edited.current = true; setInbox(e.target.value); }} placeholder="Inbox ID from AgentMail Console" /></label>
        <label>AgentMail API key<input type="password" value={key} maxLength={512} autoComplete="new-password" disabled={busy}
          onChange={e => setKey(e.target.value)} placeholder={keepSecret ? 'Leave blank to keep the saved key' : 'Inbox-scoped read-only key'} /></label>
      </>}
      <p>The credential is encrypted on Kyrex Cloud and never returned to your device. Kyrex cannot send or delete messages through this connector.</p>
      <div className="connection-actions">
        <button type="button" className="connection-btn" disabled={busy || !view || !inbox.trim() || (!key.trim() && !keepSecret)}
          onClick={() => act('/connect', provider === 'zoho' ? { provider, email: inbox.trim(), password: key.trim(), host }
            : { provider, inbox_id: inbox.trim(), api_key: key.trim() })}>{busy ? 'Saving…' : 'Connect dedicated inbox'}</button>
        {view?.connected && <button type="button" className="connection-btn secondary" disabled={busy} onClick={() => act('/disconnect', {})}>Disconnect dedicated inbox</button>}
        {view?.source === 'gmail' && <button type="button" className="connection-btn secondary" disabled={busy} onClick={() => act('/source', { source: 'none' })}>Stop personal Gmail reads</button>}
      </div>
    </div>
    {view?.active && <div>
      <p>Forward selected emails to <strong style={{ overflowWrap: 'anywhere' }}>{view.address}</strong>.</p>
      <button type="button" className="connection-btn secondary" onClick={async () => {
        try { await navigator.clipboard.writeText(view.address); setSaved('Inbox address copied.'); }
        catch { setError('Select and copy the inbox address above.'); }
      }}>Copy inbox address</button>
      <ol>
        <li>In Gmail Settings → Forwarding and POP/IMAP, add this forwarding address.</li>
        <li>Ask Email Bot to read the forwarding confirmation email and open its verification link yourself.</li>
        <li>Create Gmail filters for selected school senders or topics and choose “Forward it to” this address. Keep forwarding for all incoming mail turned off.</li>
      </ol>
      <p>Then ask Email Bot to read or summarize the forwarded emails. Automatic forwarding is best for sender notification rules; manually forwarded messages may show you as the sender.</p>
    </div>}
    {view && <details><summary>Personal Gmail access</summary>
      <p>{view.personal_gmail_enabled ? 'Personal Gmail is selected; its Google read permission is required.' : 'Personal Gmail reads are blocked in Kyrex.'} Google may still hold your earlier authorization. To revoke it at Google, remove Kyrex’s Google authorization and reconnect Calendar only. Messages already in Chat are retained.</p>
      {view.source !== 'gmail' && <>
        <p>Selecting personal Gmail allows Email Bot to read your personal mailbox when Gmail read permission is granted.</p>
        <button type="button" className="connection-btn secondary" disabled={busy} onClick={() => act('/source', { source: 'gmail' })}>Use personal Gmail</button>
      </>}
    </details>}
    {saved && <p role="status">{saved}</p>}
    {error && <p className="message-error" role="alert">{error}</p>}
  </details>;
}
