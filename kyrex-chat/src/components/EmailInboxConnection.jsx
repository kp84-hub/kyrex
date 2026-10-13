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
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [saved, setSaved] = useState('');
  const alive = useRef(true), edited = useRef(false);
  const apply = value => {
    if (!alive.current) return;
    setView(value);
    if (!edited.current) setInbox(value.inbox_id || '');
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
  return <details className="connection-card" aria-label="Email inbox settings">
    <summary>Email inbox · {view?.active ? view.address : view?.source === 'gmail' ? 'Personal Gmail selected' : view ? 'Reading disabled' : 'Loading…'}</summary>
    <p>Give Kyrex its own inbox and forward only the school and other emails you want it to read.</p>
    <p>Connecting AgentMail blocks personal Gmail reads in Kyrex and keeps your Calendar connection. If this inbox fails or is disconnected, Kyrex will not fall back to personal Gmail.</p>
    <div className="voice-settings">
      <p>Create a dedicated inbox in <a href="https://console.agentmail.to" target="_blank" rel="noreferrer">AgentMail Console</a>. Use an API key scoped to that inbox with only <code>inbox_read</code> and <code>message_read</code> permissions.</p>
      <label>AgentMail inbox ID<input value={inbox} maxLength={254} autoComplete="off" disabled={busy}
        onChange={e => { edited.current = true; setInbox(e.target.value); }} placeholder="Inbox ID from AgentMail Console" /></label>
      <label>AgentMail API key<input type="password" value={key} maxLength={512} autoComplete="new-password" disabled={busy}
        onChange={e => setKey(e.target.value)} placeholder={view?.connected && inbox === view.inbox_id ? 'Leave blank to keep the saved key' : 'Inbox-scoped read-only key'} /></label>
      <p>The key is encrypted on Kyrex Cloud and never returned to your device. Kyrex only reads this inbox; it cannot send or delete messages through this connector.</p>
      <div className="connection-actions">
        <button type="button" className="connection-btn" disabled={busy || !view || !inbox.trim() || (!key.trim() && !(view.connected && inbox.trim() === view.inbox_id))}
          onClick={() => act('/connect', { inbox_id: inbox.trim(), api_key: key.trim() })}>{busy ? 'Saving…' : 'Connect dedicated inbox'}</button>
        {view?.connected && <button type="button" className="connection-btn secondary" disabled={busy} onClick={() => act('/disconnect', {})}>Disconnect AgentMail</button>}
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
