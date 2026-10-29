import React, { useEffect, useState } from 'react';
import { accountRequest } from '../lib/api.js';
import { reserveConsentWindow, consentUrl, navigateConsentWindow, closeConsentWindow } from '../lib/consentWindow.js';

const base = '/api/gmail-cleanup';
export function GmailCleanupSettings() {
  const [view, setView] = useState(null), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [pending, setPending] = useState(null);
  const acceptConnection = value => {
    if (typeof value.connected !== 'boolean' || typeof value.address !== 'string' || typeof value.revision !== 'string') throw new Error('Gmail cleanup status is unavailable.');
    setView(value);
    setPending(old => old && (!value.connected || old.revision===value.revision) ? old : null);
    return value;
  };
  const load = async () => {
    const value = await accountRequest(base);
    return acceptConnection(value);
  };
  useEffect(() => {
    let alive = true;
    const refresh = () => accountRequest(base).then(v => { if (alive) acceptConnection(v); })
      .catch(e => { if (alive) setError(e.message); });
    refresh(); window.addEventListener('focus', refresh);
    return () => { alive = false; window.removeEventListener('focus', refresh); };
  }, []);
  const connect = async () => {
    if (busy) return;
    const popup = reserveConsentWindow(); setBusy(true); setError('');
    try {
      const result = await accountRequest(base + '/connect', 'POST', {});
      const url = consentUrl(result.authorization_url); navigateConsentWindow(popup, url); setPending({url,revision:view?.revision || ''});
    } catch (e) { closeConsentWindow(popup); setError(e.message); }
    finally { setBusy(false); }
  };
  const disconnect = async () => {
    setBusy(true); setError('');
    try { setView(await accountRequest(base + '/disconnect', 'POST', {})); setPending(null); }
    catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };
  return <section className="email-automation-settings" aria-label="Gmail cleanup">
    <h3>Gmail cleanup</h3>
    <p>Preview emails from an exact sender, then approve moving the listed messages to Trash. No email body or AI model is needed.</p>
    <p>Google requires its broad Gmail modify permission, which also permits reading and sending. Kyrex limits this separate cleanup connection to metadata reads and approved Trash operations. Budget Bot and your existing email readers keep their read-only connections.</p>
    {view?.connected ? <p>Cleanup mailbox: <strong>{view.address}</strong></p> : <p>Cleanup is not connected.</p>}
    <div className="approval-actions"><button className="approval-btn" disabled={busy} onClick={connect}>{view?.connected ? 'Reconnect cleanup mailbox' : 'Connect Gmail cleanup'}</button>
      {view?.connected && <button className="approval-btn" disabled={busy} onClick={disconnect}>Disconnect Gmail cleanup</button>}
      <button className="approval-btn" disabled={busy} onClick={() => load().catch(e => setError(e.message))}>Refresh cleanup connection</button></div>
    {pending && <p>Finish Google authorization, then refresh the connection. <a href={pending.url} target="_blank" rel="noreferrer">Open Google sign-in</a></p>}
    <p>After connecting, ask an eligible assistant: “Delete all emails from offers@example.com.” Brand names lead to a sender lookup and an exact-address question. Each preview contains at most 100 messages; archived mail can be included. No permanent-delete action is available.</p>
    {error && <p role="alert" className="message-error">{error}</p>}
  </section>;
}

export function GmailCleanupCard({ id }) {
  const [view, setView] = useState(null), [busy, setBusy] = useState(false), [error, setError] = useState(''), [tick, setTick] = useState(0);
  const accept = value => {
    if (!value || typeof value.state !== 'string' || !Array.isArray(value.messages)
      || typeof value.detail !== 'string' || typeof value.mailbox !== 'string' || typeof value.sender !== 'string'
      || typeof value.revision !== 'string' || !Number.isInteger(value.count)
      || value.messages.some(m => !m || typeof m.id !== 'string' || typeof m.subject !== 'string' || typeof m.date !== 'string')) throw new Error('Cleanup review is unavailable. Refresh its status.');
    setView(value);
  };
  useEffect(() => {
    let alive = true;
    accountRequest(base + '/reviews/' + encodeURIComponent(id)).then(v => { if (alive) { accept(v); setError(''); } }).catch(e => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [id, tick]);
  useEffect(() => {
    if (!view || !['queued', 'preparing', 'ready', 'unknown'].includes(view.state)) return;
    if (view.state === 'unknown' && view.task_status !== 'running') return;
    const timer = setTimeout(() => setTick(t => t + 1), 2500); return () => clearTimeout(timer);
  }, [view, busy]);
  const decide = async decision => {
    if (busy || view?.state !== 'ready' || !view.approval_id) return;
    setBusy(true); setError('');
    try { accept(await accountRequest(base + '/reviews/' + encodeURIComponent(id) + '/decision', 'POST',
      { revision: view.revision, approval_id: view.approval_id, decision })); setTick(t => t + 1); }
    catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };
  return <div className="approval-card" aria-label="Gmail cleanup review">
    <h4>Email cleanup review</h4><p role="status">{view?.detail || 'Loading cleanup preview…'}</p>
    {view && <><p>Mailbox: <strong>{view.mailbox}</strong><br />Exact sender: <strong>{view.sender}</strong></p>
      <p>{view.count} listed messages. {view.more ? 'More matches may remain; approval covers only this list.' : ''}</p>
      <div style={{ maxHeight: 360, overflowY: 'auto', overflowWrap: 'anywhere' }}><ol>{view.messages.map(m => <li key={m.id}>{m.subject || '(No subject)'}<br /><small>{m.date}</small></li>)}</ol></div>
      {view.state === 'ready' && view.approval_id && <div className="approval-actions">
        <button className="approval-btn approve" disabled={busy} onClick={() => decide('trash')}>Move listed emails to Trash</button>
        <button className="approval-btn deny" disabled={busy} onClick={() => decide('cancel')}>Cancel cleanup</button></div>}
    </>}
    <button className="approval-btn" disabled={busy} onClick={() => setTick(t => t + 1)}>Refresh cleanup status</button>
    {error && <p role="alert" className="message-error">{error}</p>}
  </div>;
}
