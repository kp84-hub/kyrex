import React, { useEffect, useState } from 'react';
import { fetchMessagesStatus } from '../lib/api.js';

const labels = {
  ready: 'Messages connected',
  reconnecting: 'Messages reconnecting…',
  needs_attention: 'Messages needs attention — open the phone companion to reconnect.',
  offline: 'Phone not reachable — open the Kyrex Messages companion.',
  unknown: 'Live status unavailable — update and open the phone companion.',
  not_linked: 'Phone not linked',
};

// Only mounted inside the open Messages settings panel. A saved snapshot
// remains readable even after the phone stops answering.
export default function MessagesStatus() {
  const [result, setResult] = useState(null);
  const [receivedAt, setReceivedAt] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true, inFlight = false, controller, deadline;
    const refresh = async () => {
      if (!alive || document.hidden || inFlight) return;
      inFlight = true;
      controller = new AbortController();
      deadline = setTimeout(() => controller.abort(), 10000);
      try {
        const data = await fetchMessagesStatus(controller.signal);
        if (alive) { setResult(data); setReceivedAt(Date.now()); setNow(Date.now()); setFailed(false); }
      } catch {
        if (alive && !document.hidden) setFailed(true);
      } finally { inFlight = false; clearTimeout(deadline); }
    };
    refresh();
    const poll = setInterval(refresh, 5000);
    // Expire a green status locally even if a network request hangs.
    const clock = setInterval(() => { if (alive) setNow(Date.now()); }, 1000);
    document.addEventListener('visibilitychange', refresh);
    return () => {
      alive = false; controller?.abort(); clearTimeout(deadline);
      clearInterval(poll); clearInterval(clock);
      document.removeEventListener('visibilitychange', refresh);
    };
  }, []);
  const phone = result?.phone;
  let state = phone?.status;
  if (['ready', 'reconnecting', 'needs_attention'].includes(state) &&
      now - receivedAt >= Math.max(0, phone.expires_in || 0) * 1000) state = 'offline';
  const label = failed ? 'Could not check live phone status.'
    : labels[state] || 'Checking live phone status…';
  return <div aria-label="Live Messages connection">
    <p role="status"><strong>Live phone status: </strong>{label}</p>
    {!failed && state === 'ready' ? <p>{phone.send_ready
      ? 'Ready for sends you confirm in Chat.' : 'Chat sending is off.'}</p> : null}
    {phone?.last_seen ? <p>Last phone check-in: {new Date(phone.last_seen * 1000).toLocaleString()}</p> : null}
    {result?.connected ? <p>Saved texts are available. Last synced: {new Date(result.synced_at * 1000).toLocaleString()}</p> : null}
  </div>;
}
