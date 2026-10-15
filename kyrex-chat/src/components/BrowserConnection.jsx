import React, { useEffect, useRef, useState } from 'react';
import { fetchBrowserDiagnostics, startBrowserDiagnostic } from '../lib/api.js';

const LABELS = { connection: 'Host connection', dispatch: 'Task delivery', browser_launch: 'Browser launch', site_access: 'Website access', sign_in: 'Member sign-in' };
const STATES = { passed: 'Passed', failed: 'Failed', not_checked: 'Not checked' };
const active = r => r && ['pending', 'executing'].includes(r.status);

export default function BrowserConnection() {
  const [data, setData] = useState({ bots: [], reports: [] });
  const [bot, setBot] = useState('');
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const retry = useRef(null);
  const mounted = useRef(false);

  const refresh = async () => {
    try {
      const next = await fetchBrowserDiagnostics();
      if (!Array.isArray(next?.bots) || !Array.isArray(next?.reports)) throw new Error('Browser diagnostics unavailable');
      if (mounted.current) { setData(next); setError(''); }
    } catch {
      if (mounted.current) setError('Browser diagnostics could not be loaded. Refresh to check the saved result.');
    }
  };
  useEffect(() => {
    mounted.current = true;
    refresh();
    window.addEventListener('focus', refresh);
    return () => { mounted.current = false; window.removeEventListener('focus', refresh); };
  }, []);
  const running = data.reports.some(active);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(refresh, 1500);
    return () => clearInterval(timer);
  }, [running]);

  const start = async () => {
    const selected = bot || (data.bots.length === 1 ? data.bots[0].id : '');
    if (!selected || !url.trim()) return;
    const key = JSON.stringify([selected, url.trim()]);
    if (retry.current?.key !== key) retry.current = { key, request_id: crypto.randomUUID() };
    setBusy(true); setError('');
    try {
      const report = await startBrowserDiagnostic(selected, url.trim(), retry.current.request_id);
      retry.current = null;
      if (mounted.current) setData(old => ({ ...old, reports: [report, ...old.reports.filter(r => r.reference !== report.reference)].slice(0, 10) }));
    } catch (e) {
      if (mounted.current) setError(e?.status && e.status < 500 ? e.message : 'Could not confirm the test request. Refresh for its saved result, or retry this same test.');
    } finally { if (mounted.current) setBusy(false); }
  };

  return <details className="connection-card connector-card browser-connection" aria-label="Browser connection">
    <summary className="connector-row"><span className="connector-identity"><strong>Browser</strong><span className="connector-category">Test website access and see saved failures</span></span></summary>
    <div className="connector-detail-panel">
      <p>Tests open and inspect an allowed website using your selected Browser Bot. They do not click controls or submit a booking.</p>
      {data.bots.length ? <>
        <label>Browser Bot<select aria-label="Browser Bot to test" value={bot || (data.bots.length === 1 ? data.bots[0].id : '')} onChange={e => setBot(e.target.value)}>
          <option value="">Choose a Browser Bot</option>{data.bots.map(b => <option key={b.id} value={b.id}>{b.name} · {b.connected ? 'Host connected' : b.available ? 'Host online' : 'Host unavailable'}</option>)}
        </select></label>
        <label>Website<input type="url" aria-label="Website to test" placeholder="https://example.com" value={url} onChange={e => setUrl(e.target.value)} /></label>
        <p>Use the page you want to check, without sign-in tokens or action links. Sign-in checks are available for supported member portals.</p>
        <div className="connection-actions"><button className="connection-btn" disabled={busy || running || !url.trim() || !(bot || data.bots.length === 1)} onClick={start}>{busy || running ? 'Testing browser…' : 'Test browser connection'}</button></div>
      </> : <p>Configure a Browser Bot in Bots to test its connection.</p>}
      <button className="connection-btn" onClick={refresh}>Refresh browser status</button>
      {error ? <p className="message-error" role="alert">{error}</p> : null}
      {data.reports.length ? <div className="browser-test-results" aria-live="polite">
        <h4>Recent checks and failures</h4>
        {data.reports.map((report, index) => <details key={report.reference} className="browser-test-report" open={index === 0 || active(report) || undefined}>
          <summary>{active(report) ? 'Testing browser…' : report.code ? 'Browser check failed' : 'Browser check completed'} · {report.reference}</summary>
          <p>{report.message}</p>
          <p>{report.bot_id} · {report.host_id || 'No host selected'} · {report.created_at ? new Date(report.created_at).toLocaleString() : ''}</p>
          <ul>{(report.stages || []).map(s => <li key={s.id}>{LABELS[s.id] || s.id}: {STATES[s.state] || 'Not checked'}</li>)}</ul>
          <p>Cloud version: {report.cloud_build || 'unknown'} · Host version: {report.host_build || 'unknown'}</p>
          <p>Diagnostic: <code>{report.reference}</code></p>
        </details>)}
      </div> : null}
    </div>
  </details>;
}
