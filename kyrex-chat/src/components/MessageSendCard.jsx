import React, { useEffect, useState } from 'react';
import { fetchMessageSend, decideMessageSend, fetchMessageReply } from '../lib/api.js';

const active = new Set(['queued', 'preparing', 'ready', 'send_queued', 'sending']);
const labels = {
  queued: 'Waiting for phone. Keep the Kyrex Messages companion open.',
  preparing: 'Phone is verifying recipients. Nothing sent.',
  ready: 'Review every recipient and the exact message before sending.',
  send_queued: 'Confirmed. Waiting for phone to send once.',
  sending: 'Sending once. Please wait; do not retry.',
  accepted: 'Google Messages accepted the send. This does not confirm delivery.',
  unknown: 'Send outcome unknown. Check Google Messages before trying again. No automatic retry.',
  failed: 'Phone could not prepare the message. Nothing sent.',
  cancelled: 'Cancelled. No send will be retried.',
  expired: 'Preview or phone request expired. Prepare a new message; nothing will be retried.',
};
export default function MessageSendCard({ id }) {
  const [job, setJob] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [reply, setReply] = useState('');
  const [refreshTick, setRefreshTick] = useState(0);
  useEffect(() => {
    let alive = true, timer, refreshing = false;
    const refresh = async () => {
      if (!alive || refreshing) return;
      clearTimeout(timer); refreshing = true;
      try {
        const result = await fetchMessageSend(id);
        if (!alive) return;
        setJob(result); setError('');
        if (active.has(result.state)) timer = setTimeout(refresh, 2000);
      } catch (e) { if (alive) setError(e.message || 'Could not check message status.'); }
      finally { refreshing = false; }
    };
    const resume = () => { if (document.visibilityState !== 'hidden') refresh(); };
    window.addEventListener('focus', resume);
    window.addEventListener('online', resume);
    document.addEventListener('visibilitychange', resume);
    refresh();
    return () => {
      alive = false; clearTimeout(timer);
      window.removeEventListener('focus', resume);
      window.removeEventListener('online', resume);
      document.removeEventListener('visibilitychange', resume);
    };
  }, [id, refreshTick]);
  const decide = async (decision) => {
    if (busy) return;
    setBusy(true); setError('');
    try { setJob(await decideMessageSend(id, decision)); }
    catch (e) { setError(e.message || 'Could not record your decision. Check status before retrying.'); }
    finally { setBusy(false); }
  };
  const readReply = async () => {
    setBusy(true); setError('');
    try { setReply((await fetchMessageReply(id)).content); }
    catch (e) { setError(e.message || 'Could not read the reply.'); }
    finally { setBusy(false); }
  };
  return <div className="approval-card" aria-label="Message send">
    <p role="status">{job ? labels[job.state] || 'Checking message status…' : 'Loading message preview…'}</p>
    {job ? <>
      <strong>{job.name}</strong>
      {job.recipients.length ? <div>To: <ul>{job.recipients.map((recipient, i) => <li key={i}>{recipient}</li>)}</ul></div> : null}
      <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{job.text}</div>
      {job.state === 'ready' ? <div className="approval-actions">
        <button className="approval-btn approve" disabled={busy || Boolean(error)} onClick={() => decide('send')}>Send</button>
        <button className="approval-btn deny" disabled={busy} onClick={() => decide('cancel')}>Cancel</button>
      </div> : ['queued','preparing','send_queued'].includes(job.state) ? <button disabled={busy} onClick={() => decide('cancel')}>Cancel</button> : null}
      {['accepted', 'unknown'].includes(job.state) ? <button className="approval-btn" disabled={busy} onClick={readReply}>Read reply</button> : null}
    </> : null}
    {reply ? <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }} aria-label="Latest reply">{reply}</div> : null}
    {error ? <><p className="message-error">{error}</p><button disabled={busy} onClick={() => setRefreshTick(t => t + 1)}>Check status</button></> : null}
  </div>;
}
