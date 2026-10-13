import React, { useEffect, useState } from 'react';
import { fetchEmailDraft, editEmailDraft, decideEmailDraft, fetchEmailReplies } from '../lib/api.js';

export default function EmailDraftCard({ id }) {
  const [job, setJob] = useState(null);
  const [fields, setFields] = useState({ to: '', subject: '', body: '' });
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [reply, setReply] = useState('');
  const [tick, setTick] = useState(0);
  const accept = value => {
    setJob(value);
    setFields({ to: value.to, subject: value.subject, body: value.body });
    setDirty(false);
  };
  useEffect(() => {
    let alive = true;
    fetchEmailDraft(id).then(value => { if (alive) { accept(value); setError(''); } })
      .catch(e => { if (alive) setError(e.message || 'Could not load email review.'); });
    return () => { alive = false; };
  }, [id, tick]);
  useEffect(() => {
    if (job?.state !== 'sending') return;
    const timer = setTimeout(() => setTick(t => t + 1), 2000);
    return () => clearTimeout(timer);
  }, [job]);
  const change = (key, value) => { setFields(f => ({ ...f, [key]: value })); setDirty(true); };
  const save = async () => {
    if (busy) return;
    setBusy(true); setError('');
    try { accept(await editEmailDraft(id, job.revision, fields)); }
    catch (e) { setError(e.message || 'Could not save changes. Reload this review before sending.'); }
    finally { setBusy(false); }
  };
  const decide = async decision => {
    if (busy || (decision === 'send' && dirty) || job.state !== 'draft') return;
    setBusy(true); setError('');
    const revision = job.revision;
    if (decision === 'send') setJob(j => ({ ...j, state: 'sending', detail: 'Sending once. Do not retry.' }));
    try { accept(await decideEmailDraft(id, revision, decision)); }
    catch (e) { setError(e.message || 'Could not confirm the result. Check status before retrying.'); }
    finally { setBusy(false); }
  };
  const readReply = async () => {
    if (busy) return;
    setBusy(true); setError('');
    try { setReply((await fetchEmailReplies(id)).content); }
    catch (e) { setError(e.message || 'Could not check replies.'); }
    finally { setBusy(false); }
  };
  const editable = job?.state === 'draft' && !busy;
  return <div className="approval-card email-draft-card" aria-label="Email review">
    <p role="status">{job?.detail || 'Loading email review…'}</p>
    {job ? <>
      <p><strong>From:</strong> <span style={{ overflowWrap: 'anywhere' }}>{job.from}</span></p>
      {job.reply ? <p>Threaded reply · Original sender: {job.original_sender}</p> : null}
      <label>To<input type="email" value={fields.to} maxLength={254} disabled={!editable || job.reply}
        onChange={e => change('to', e.target.value)} /></label>
      <label>Subject<input value={fields.subject} maxLength={200} disabled={!editable}
        onChange={e => change('subject', e.target.value)} /></label>
      <label>Message<textarea value={fields.body} rows={8} maxLength={24000} disabled={!editable}
        onChange={e => change('body', e.target.value)} /></label>
      {job.state === 'draft' ? <div className="approval-actions">
        {dirty ? <button className="approval-btn" disabled={busy} onClick={save}>Save changes</button> : null}
        <button className="approval-btn approve" disabled={busy || dirty} onClick={() => decide('send')}>Send email</button>
        <button className="approval-btn deny" disabled={busy} onClick={() => decide('cancel')}>Cancel email</button>
      </div> : null}
      {dirty ? <p>Save your changes, review the saved message, then press Send email.</p> : null}
      {['accepted', 'unknown'].includes(job.state) ? <button className="approval-btn" disabled={busy} onClick={readReply}>Check email replies</button> : null}
    </> : null}
    {reply ? <div aria-label="Email reply" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{reply}</div> : null}
    {error ? <p className="message-error" role="alert">{error}</p> : null}
    <button className="approval-btn" disabled={busy} onClick={() => setTick(t => t + 1)}>Refresh email status</button>
  </div>;
}
