import React, { useEffect, useState } from 'react';
import { composioRequest, remoteMCPRequest } from '../lib/api.js';

export default function ComposioApps({ onChange }) {
  const [data, setData] = useState({ configured: false, apps: [], connections: [] });
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [link, setLink] = useState(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  async function refresh() {
    setData(await composioRequest());
    setLoaded(true);
  }
  useEffect(() => {
    let active = true;
    composioRequest().then(value => { if (active) { setData(value); setLoaded(true); } })
      .catch(() => { if (active) { setLoaded(true); setError('App connections could not be loaded.'); } });
    return () => { active = false; };
  }, []);

  async function run(action, success) {
    setBusy(true); setError(''); setNotice('');
    try { await action(); await refresh(); await onChange?.(); setNotice(success); }
    catch (e) { setError(e.status === 401 ? 'Sign in again to manage app connections.' : e.message || 'Could not update the app connection.'); }
    finally { setBusy(false); }
  }

  async function connect(slug) {
    setLink(null);
    await run(async () => {
      const result = await composioRequest('POST', '/apps/' + slug, {});
      const url = new URL(result.redirect_url);
      if (url.origin !== 'https://connect.composio.dev' || !url.pathname.startsWith('/link/')) {
        throw new Error('The app returned an unsupported sign-in link.');
      }
      setLink({ id: result.id, url: url.href });
    }, 'Open the sign-in link, authorize the account, then return and check the connection.');
  }

  const pending = (data.connections || []).filter(row => row.setup_status !== 'ready');
  return <details className="connection-card connector-card" aria-label="Apps via Composio">
    <summary className="connector-row"><span className="connector-identity">
      <strong>Apps via Composio</strong><span className="connector-category">Connect an app · Read-only</span>
    </span></summary>
    <div className="connector-detail-panel">
      <p>Sign in to an app, then choose the read tools Kyrex may use below. Composio stores and refreshes the app authorization you grant.</p>
      <p>Only selected read tools are available in Kyrex. Review the provider's consent screen: the account authorization may include broader scopes. Existing Kyrex connections are managed separately.</p>
      {!loaded && <p role="status">Loading app connections…</p>}
      {loaded && !data.configured && !error && <p>App sign-in needs server setup. The operator must configure a Composio project API key.</p>}
      {(data.apps || []).map(app => <section key={app.slug} aria-label={app.name + ' app'}>
        <h4>{app.name}</h4><p>{app.description}</p>
        <button type="button" className="connection-btn" disabled={busy || !data.configured}
          onClick={() => connect(app.slug)}>Connect {app.name}</button>
      </section>)}
      {link && <p><a href={link.url} target="_blank" rel="noopener noreferrer">Continue app sign-in</a></p>}
      {pending.map(row => <section key={row.id} aria-label={'Finish ' + row.label}>
        <h4>{row.label}</h4>
        <p>Waiting for sign-in. Setup expires after 15 minutes; an expired setup can be removed and started again.</p>
        <button type="button" className="connection-btn" disabled={busy} onClick={() => run(async () => {
          await composioRequest('POST', '/' + row.id + '/complete', {});
          setLink(null);
        }, 'Account connected. Select read-only tools below and save to enable them.')}>I finished signing in</button>
        <button type="button" className="connection-btn secondary" disabled={busy} onClick={() => run(async () => {
          await remoteMCPRequest('DELETE', '/' + row.id);
          if (link?.id === row.id) setLink(null);
        }, 'App setup removed.')}>Cancel setup</button>
      </section>)}
      {busy && <p role="status">Checking app connection…</p>}
      {notice && <p role="status">{notice}</p>}
      {error && <p role="alert">{error}</p>}
    </div>
  </details>;
}
