import React, { useEffect, useState } from 'react';
import { eventReviewRequest } from '../lib/api.js';
import { clearReviewedNotifications } from '../lib/notifications.js';

export default function EventSuggestionCard({ id, onResolved }) {
  const [job, setJob] = useState(null), [fields, setFields] = useState(null), [editing, setEditing] = useState(false);
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [tick, setTick] = useState(0);
  const path = '/suggestions/' + encodeURIComponent(id);
  const accept = value => { setJob(value); if (!editing) setFields(value.fields); };
  useEffect(() => {
    let alive = true;
    eventReviewRequest(path).then(value => { if (alive) { accept(value); setError(''); } })
      .catch(e => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [id, tick]);
  useEffect(() => {
    if (!job || !['queued','running','awaiting_approval'].includes(job.task?.status)) return undefined;
    const timer = setTimeout(() => setTick(t => t + 1), 3000);
    return () => clearTimeout(timer);
  }, [job]);
  useEffect(() => {
    if (!['added', 'dismissed', 'duplicate'].includes(job?.state)) return;
    clearReviewedNotifications(job.notification_ids);
    onResolved?.(id);
  }, [job, id, onResolved]);
  const action = async (name, payload = {}) => {
    if (busy || !job) return;
    setBusy(true); setError('');
    try {
      const value = await eventReviewRequest(path + '/' + name, 'POST', { revision: job.revision, ...payload });
      setJob(value); setFields(value.fields); setEditing(false);
    } catch (e) { setError(e.message); setTick(t => t + 1); }
    finally { setBusy(false); }
  };
  const change = (key, value) => setFields(f => ({ ...f, [key]: value }));
  if (['added', 'dismissed', 'duplicate'].includes(job?.state)) return null;
  return <article className="approval-card event-suggestion-card" aria-label="Calendar suggestion">
    {!job && !error && <p role="status">Loading calendar suggestion…</p>}
    {job && <>
      <strong>{job.fields.title}</strong>
      <p>{job.fields.date || 'Date needed'} · {job.fields.all_day ? 'All day' : `${job.fields.start || 'Start needed'} – ${job.fields.end || 'End needed'}`} · {job.timezone}</p>
      {job.fields.location && <p>Location: {job.fields.location}</p>}
      <p>Source: {job.source.label}{job.source.received ? ` · ${new Date(job.source.received).toLocaleString()}` : ''}</p>
      <details><summary>Source evidence and coverage</summary><blockquote>{job.evidence}</blockquote><p>{job.source.coverage}</p>
        {job.source.synced_at && <p>Phone synced: {new Date(job.source.synced_at * 1000).toLocaleString()}</p>}</details>
      <p>Calendar: {job.destination.calendar} · {job.destination.email}</p>
      {job.needs.length > 0 && <p>Needs attention: {job.needs.join(', ')}.</p>}
      {job.kind !== 'create' && <p>This {job.kind === 'cancel' ? 'cancellation' : 'schedule change'} needs a review of the existing calendar event. Open Calendar to update it.</p>}
      {job.detail && <p role="status">{job.detail}</p>}
      {editing && fields && <div className="email-automation-form">
        <label>Title<input value={fields.title} maxLength={200} onChange={e => change('title', e.target.value)} /></label>
        <label>Date<input type="date" value={fields.date} onChange={e => change('date', e.target.value)} /></label>
        <label><span><input type="checkbox" checked={fields.all_day} onChange={e => change('all_day', e.target.checked)} /> All day</span></label>
        {!fields.all_day && <><label>Start<input type="time" value={fields.start} onChange={e => change('start', e.target.value)} /></label>
          <label>End<input type="time" value={fields.end} onChange={e => change('end', e.target.value)} /></label></>}
        <label>Location<input value={fields.location} maxLength={500} onChange={e => change('location', e.target.value)} /></label>
        <button type="button" disabled={busy} onClick={() => action('edit', { fields })}>Save event details</button>
        <button type="button" disabled={busy} onClick={() => { setFields(job.fields); setEditing(false); }}>Cancel edit</button>
      </div>}
      {!editing && job.state === 'pending' && <div className="approval-actions">
        {job.kind === 'create' && <>
          <button type="button" disabled={busy || job.needs.length > 0} onClick={() => action('review')}>Review calendar event</button>
          {job.can_edit && <button type="button" disabled={busy} onClick={() => setEditing(true)}>Edit</button>}
        </>}
        <button type="button" disabled={busy} onClick={() => action('dismiss')}>Dismiss</button>
      </div>}
      {job.state === 'ended' && job.can_edit && !editing && <button type="button" disabled={busy} onClick={() => setEditing(true)}>Edit for a new review</button>}
      {job.state === 'blocked' && <button type="button" disabled={busy} onClick={() => action('dismiss')}>Dismiss</button>}
      {job.state === 'reviewing' && <p role="status">{job.task?.status === 'awaiting_approval' ? 'Review the exact calendar payload below, then approve or deny.' : 'Preparing the calendar review…'}</p>}
      {job.approval && <div className="event-exact-review"><pre>{job.approval.detail}</pre><div className="approval-actions">
        <button type="button" className="approval-btn approve" disabled={busy} onClick={() => action('decision', { approval_id: job.approval.approval_id, decision: 'approve' })}>Approve</button>
        <button type="button" className="approval-btn deny" disabled={busy} onClick={() => action('decision', { approval_id: job.approval.approval_id, decision: 'deny' })}>Deny</button>
      </div></div>}
      {['added','dismissed','duplicate'].includes(job.state) && <p role="status">{job.state === 'added' ? 'Added to Calendar' : job.state === 'dismissed' ? 'Dismissed' : 'Already on Calendar'}</p>}
    </>}
    {error && <p role="alert" className="message-error">{error}</p>}
    <button type="button" disabled={busy} onClick={() => setTick(t => t + 1)}>Refresh status</button>
  </article>;
}
