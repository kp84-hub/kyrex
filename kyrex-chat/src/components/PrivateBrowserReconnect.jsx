import React, {useEffect, useRef, useState} from 'react';
import {controlPrivateBrowser, fetchPrivateBrowser} from '../lib/api.js';

const waiting = state => ['starting', 'ready', 'pending', 'stopping'].includes(state);
function privateUrl(value) {
  try {
    const u = new URL(value);
    return u.protocol === 'https:' && u.hostname.endsWith('.ts.net') && !u.username && !u.password
      && !u.search && !u.hash && (!u.port || u.port === '443') && u.pathname === '/vnc.html' ? value : '';
  } catch { return ''; }
}

export default function PrivateBrowserReconnect({botId, url, available}) {
  const [session, setSession] = useState({state: 'idle'});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const retry = useRef(null);
  const generation = useRef(0);
  useEffect(() => {
    const current = ++generation.current;
    setSession({state: 'idle'}); setBusy(false); setError(''); retry.current = null;
    if (available && botId) {
      fetchPrivateBrowser(botId).then(result => {
        if (generation.current === current) setSession(result);
      }).catch(() => {
        if (generation.current === current) setError('Private sign-in status could not be loaded.');
      });
    }
    return () => { generation.current++; };
  }, [botId, available]);
  useEffect(() => {
    if (!available || !botId || !waiting(session.state)) return;
    let live = true;
    const current = generation.current;
    const timer = setInterval(() => {
      fetchPrivateBrowser(botId).then(result => {
        if (live && generation.current === current) setSession(result);
      }).catch(() => {
        if (live && generation.current === current) setError('Sign-in status is unavailable. Your window has not been restarted.');
      });
    }, 2000);
    return () => { live = false; clearInterval(timer); };
  }, [available, botId, session.state]);

  const act = async action => {
    const current = generation.current;
    const details = {bot_id: botId, url: action === 'start' ? url.trim() : '', action,
      lease: action === 'end' ? session.lease : ''};
    const key = JSON.stringify(details);
    if (retry.current?.key !== key) retry.current = {key, request_id: crypto.randomUUID()};
    setBusy(true); setError('');
    try {
      const result = await controlPrivateBrowser({...details, request_id: retry.current.request_id});
      if (generation.current !== current) return;
      retry.current = null; setSession(result);
      if (result.state === 'busy') setError('Finish the current browser task or private viewer first.');
      if (result.state === 'unavailable') setError('The private sign-in service is unavailable. Check the Browser Host setup.');
    } catch {
      if (generation.current === current) setError('Could not confirm the sign-in request. Retry to check the same request.');
    } finally { if (generation.current === current) setBusy(false); }
  };
  if (!available) return <p>Private sign-in recovery requires the Browser Host reconnect service to be configured.</p>;
  const link = privateUrl(session.viewer_url);
  return <section aria-label="Private browser sign-in">
    <h4>Browser sign-in</h4>
    <p>Sign in in your Browser Host’s private window. Your password and browser cookies stay on that host.
      Saved sign-ins are preserved across task restarts; websites can still expire them.</p>
    {!waiting(session.state) ? <button className="connection-btn" disabled={busy || !botId || !url.trim()}
      onClick={() => act('start')}>Reconnect sign-in</button> : null}
    {waiting(session.state) ? <>
      <p role="status">{session.state === 'stopping' ? 'Closing the private window and saving your sign-in…'
        : 'Private sign-in window requested. Connect Tailscale on this device and use your existing viewer password.'}</p>
      {link && session.state === 'ready' ? <a className="connection-btn" href={link} target="_blank" rel="noopener noreferrer">Open private sign-in browser</a> : null}
      {session.lease && session.state !== 'stopping' ? <button className="connection-btn" disabled={busy}
        onClick={() => act('end')}>Finish sign-in and hand back</button> : null}
    </> : null}
    {session.state === 'ended' || session.state === 'idle' ? <p>After signing in and handing back, use Test browser connection to verify it. No booking is submitted by reconnecting.</p> : null}
    {error ? <p role="alert" className="message-error">{error}</p> : null}
  </section>;
}
