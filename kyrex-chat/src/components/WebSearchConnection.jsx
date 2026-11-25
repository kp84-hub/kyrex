import React, { useEffect, useState } from 'react';
import { webSearchRequest } from '../lib/api.js';

export default function WebSearchConnection() {
  const [view, setView] = useState(null);
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [removing, setRemoving] = useState(false);
  async function refresh() {
    try { setView(await webSearchRequest()); setError(''); }
    catch { setError('Web search settings could not be loaded. Try again.'); }
  }
  useEffect(() => {
    let active = true;
    webSearchRequest().then(data => { if (active) setView(data); })
      .catch(() => { if (active) setError('Web search settings could not be loaded. Try again.'); });
    return () => { active = false; };
  }, []);

  async function save(event) {
    event.preventDefault(); setBusy(true); setError(''); setNotice('');
    try {
      setView(await webSearchRequest('PUT', { key }));
      setNotice('Key saved. API access will be checked when you search.');
    } catch (e) {
      setError(e.status === 401 ? 'Sign in again to manage web search.' : 'The search key could not be saved. Check it and try again.');
    } finally { setKey(''); setBusy(false); }
  }

  async function disconnect() {
    setBusy(true); setError(''); setNotice('');
    try { setView(await webSearchRequest('DELETE')); setRemoving(false); setNotice('Web search disconnected.'); }
    catch { setError('Web search could not be disconnected. Try again.'); }
    finally { setBusy(false); }
  }

  return <details className="connection-card connector-card" aria-label="Web search connection">
    <summary className="connector-row"><span className="connector-identity"><strong>Web search</strong>
      <span className="connector-category">Tavily Search API · {view?.configured ? 'Key saved' : 'Find links for your Bots'}</span></span></summary>
    <div className="connector-detail-panel">
      <p>Search through an API, then let Browser Bot verify the websites. Search snippets alone do not confirm current stock, prices or other live details.</p>
      <p>Search queries go to Tavily. Your key is encrypted on the server and stays out of Bot prompts. Kyrex uses basic search. The free Researcher plan includes 1,000 monthly credits; keep pay-as-you-go off in Tavily to avoid usage charges.</p>
      <p><a href="https://app.tavily.com/" target="_blank" rel="noreferrer">Get a Tavily Search API key</a></p>
      {view?.requires_reconnect && <p role="status">Web search now uses Tavily. Replace your saved Brave key with a Tavily key to reconnect.</p>}
      {view && <form className="provider-form" onSubmit={save}>
        <label>Tavily Search API key<input aria-label="Tavily Search API key" type="password" value={key}
          minLength={8} maxLength={4096} autoComplete="new-password" required
          onChange={event => setKey(event.target.value)} /></label>
        <button className="connection-btn" disabled={busy || !key.trim()}>{view.configured ? 'Replace key' : 'Save key'}</button>
      </form>}
      {(view?.configured || view?.requires_reconnect) && <button type="button" className="connection-btn secondary" disabled={busy}
        onClick={() => setRemoving(true)}>Disconnect web search</button>}
      {removing && <div role="group" aria-label="Confirm web search disconnect">
        <p>Remove this key and stop API web search for your Bots?</p>
        <button type="button" disabled={busy} onClick={disconnect}>Confirm disconnect</button>
        <button type="button" disabled={busy} onClick={() => setRemoving(false)}>Keep connection</button>
      </div>}
      {!view && !error && <p role="status">Loading web search settings…</p>}
      {busy && <p role="status">Saving web search settings…</p>}
      {notice && <p role="status">{notice}</p>}
      {error && <p role="alert">{error} {!view && <button type="button" onClick={refresh}>Retry</button>}</p>}
    </div>
  </details>;
}
