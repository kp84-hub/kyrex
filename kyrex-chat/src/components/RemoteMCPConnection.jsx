import React, { useEffect, useState } from 'react';
import ComposioApps from './ComposioApps.jsx';
import { remoteMCPRequest } from '../lib/api.js';

// URLs can contain session capabilities. Keep URL/key only in the setup form;
// the server's saved views contain a hostname and selected tools, never either.
export default function RemoteMCPConnection() {
  const [connections, setConnections] = useState([]);
  const [selected, setSelected] = useState({});
  const [label, setLabel] = useState('');
  const [url, setUrl] = useState('');
  const [token, setToken] = useState('');
  const [authHeader, setAuthHeader] = useState('Authorization');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [removing, setRemoving] = useState(null);

  async function refresh() {
    const data = await remoteMCPRequest();
    const rows = data.connections || [];
    setConnections(rows);
    setSelected(Object.fromEntries(rows.map(row => [row.id, row.tools.filter(tool => tool.selected).map(tool => tool.name)])));
  }
  useEffect(() => { let active = true;
    remoteMCPRequest().then(data => {
      if (!active) return;
      const rows = data.connections || [];
      setConnections(rows);
      setSelected(Object.fromEntries(rows.map(row => [row.id, row.tools.filter(tool => tool.selected).map(tool => tool.name)])));
    }).catch(() => { if (active) setError('Remote MCP settings could not be loaded.'); });
    return () => { active = false; };
  }, []);

  async function run(action, message) {
    setBusy(true); setError(''); setNotice('');
    try { await action(); await refresh(); setNotice(message); }
    catch (e) { setError(e.status === 401 ? 'Sign in again to manage MCP connections.' : e.message || 'MCP request failed.'); }
    finally { setBusy(false); }
  }

  async function connect(event) {
    event.preventDefault();
    try {
      await run(() => remoteMCPRequest('POST', '', { label, url, token, auth_header: authHeader }),
        'Connection tested. Select read-only tools below and save to enable them.');
    } finally { setToken(''); setUrl(''); }
  }

  return <><ComposioApps onChange={refresh} /><details className="connection-card connector-card" aria-label="Remote MCP connections">
    <summary className="connector-row"><span className="connector-identity"><strong>Remote MCP</strong><span className="connector-category">Connect a tool server · Read-only</span></span></summary>
    <div className="connector-detail-panel">
      <p>Connect a trusted public MCP server, test it, then choose the read-only tools your Bots and workspace chats may use. Bot restrictions still apply.</p>
      <p>Local servers on your PC need a connected host; they cannot be reached through this form. Use Apps via Composio above for supported app sign-in.</p>
      <form className="provider-form" onSubmit={connect}>
        <label>Name<input aria-label="MCP connection name" value={label} maxLength={80} required onChange={e => setLabel(e.target.value)} /></label>
        <label>HTTPS MCP endpoint<input aria-label="MCP endpoint" type="url" value={url} required autoComplete="off" onChange={e => setUrl(e.target.value)} /></label>
        <label>Authentication<select aria-label="MCP authentication" value={authHeader} onChange={e => setAuthHeader(e.target.value)}>
          <option value="Authorization">Bearer token</option><option value="x-api-key">API key header</option>
        </select></label>
        <label>Key (optional for public servers)<input aria-label="MCP key" type="password" value={token} maxLength={4096} autoComplete="new-password" onChange={e => setToken(e.target.value)} /></label>
        <button className="connection-btn" disabled={busy}>Add and test connection</button>
      </form>
      {connections.filter(row => row.setup_status !== 'awaiting_sign_in').map(row => <section key={row.id} aria-label={`MCP ${row.label}`}>
        <h4>{row.label}</h4>
        <p>{row.host} · {row.status === 'enabled' ? 'Selected tools enabled' : 'Tested · no tools enabled'}{row.has_key ? ' · Key saved' : ''}</p>
        <p>Last tested: {new Date(row.tested_at * 1000).toLocaleString()}</p>
        {row.tools.length === 0 && <p>No tools advertised by this server.</p>}
        {row.tools.map(tool => <label key={tool.name} style={{ display: 'block', marginBottom: '0.5rem' }}>
          <input type="checkbox" disabled={busy || !tool.read_only} checked={(selected[row.id] || []).includes(tool.name)}
            onChange={e => setSelected(previous => ({ ...previous, [row.id]: e.target.checked
              ? [...(previous[row.id] || []), tool.name] : (previous[row.id] || []).filter(name => name !== tool.name) }))} />
          {' '}{tool.name}{!tool.read_only && ' · Unavailable: not explicitly read-only or schema unsupported'}
          {tool.description && <small style={{ display: 'block' }}>{tool.description}</small>}
        </label>)}
        {row.source === 'composio' && <p>Disconnect removes this Kyrex grant and attempts to retire its MCP session. To revoke the app authorization itself, also disconnect it in Composio or the provider's settings.</p>}
        <p>Tool safety labels come from the server. Only connect servers you trust. Testing lists tools; it does not run them.</p>
        <div className="connection-actions">
          <button type="button" className="connection-btn" disabled={busy} onClick={() => run(
            () => remoteMCPRequest('PUT', `/${row.id}/tools`, { tools: selected[row.id] || [] }), 'Tool selection saved.')}>Save selected tools</button>
          <button type="button" className="connection-btn secondary" disabled={busy} onClick={() => run(
            () => remoteMCPRequest('POST', `/${row.id}/test`), 'Connection tested. Changed tools need selecting again.')}>Test connection</button>
          <button type="button" className="connection-btn secondary" disabled={busy} onClick={() => setRemoving(row.id)}>Disconnect</button>
        </div>
        {removing === row.id && <div role="group" aria-label="Confirm MCP disconnect">
          <p>Disconnect this server and remove access to its tools?</p>
          <button type="button" disabled={busy} onClick={async () => {
            await run(() => remoteMCPRequest('DELETE', `/${row.id}`), 'MCP connection removed.'); setRemoving(null);
          }}>Confirm disconnect</button>
          <button type="button" disabled={busy} onClick={() => setRemoving(null)}>Keep connection</button>
        </div>}
      </section>)}
      {busy && <p role="status">Checking MCP settings…</p>}
      {notice && <p role="status">{notice}</p>}
      {error && <p role="alert">{error}</p>}
    </div>
  </details></>;
}
