import React, { useRef, useState } from 'react';
import { runLevel6Preview } from '../lib/api.js';

const activeStatuses = new Set(['queued', 'running', 'awaiting_approval']);

export default function Level6PreviewActions({ conversationId, messages, onQueued }) {
  const [busy, setBusy] = useState(false);
  const [queuedTask, setQueuedTask] = useState(null);
  const [error, setError] = useState('');
  const submitting = useRef(false);
  const active = messages.some(message => activeStatuses.has(message.task?.status));
  const savedResult = queuedTask && messages.some(message =>
    (message.id === `task-${queuedTask}-result` || message.persisted_id === `task-${queuedTask}-result`)
      && !message.task);
  const pending = Boolean(queuedTask && !savedResult);
  const run = async () => {
    if (submitting.current || active || pending) return;
    submitting.current = true; setBusy(true); setError('');
    try {
      const job = await runLevel6Preview(conversationId);
      setQueuedTask(activeStatuses.has(job.status) ? job.task_id : null);
      await onQueued(conversationId);
    } catch (e) {
      setError(e.message || 'Could not start the workout preview.');
    } finally {
      submitting.current = false; setBusy(false);
    }
  };
  return <div className="level6-preview-actions" aria-label="Workout preview controls">
    <button type="button" className="approval-btn" disabled={busy || active || pending} onClick={run}>
      {busy ? 'Queuing preview…' : active || pending ? 'Preview in progress…' : 'Run preview now'}
    </button>
    <span>Nothing sends automatically.</span>
    {error && <p role="alert">{error}</p>}
  </div>;
}
