import React from 'react';
import { isActiveActivity } from '../lib/activeWork.js';
import { progressUpdates } from '../lib/progress.js';

const LABELS = {
  queued: 'Queued',
  running: 'Working in the repository…',
  awaiting_approval: 'Waiting for your approval',
  done: 'Finished',
  failed: 'Task failed',
  cancelled: 'Task cancelled',
  unknown: 'Checking task status…',
};

export default function DeveloperActivity({ message, onReview, children }) {
  const updates = progressUpdates(message.events);
  const status = message.cancelled ? 'cancelled' : message.developer_status
    || (message.approval ? 'awaiting_approval' : message.task?.status)
    || (message.error ? 'failed' : message.streaming ? 'running' : 'done');
  const active = isActiveActivity(status);
  const label = status === 'running' && updates.length ? updates.at(-1) : LABELS[status] || LABELS.unknown;
  return (
    <section className="developer-activity" aria-label="Developer Bot activity">
      <div className="developer-activity-heading">
        <span className={`task-dot task-${status}`} aria-hidden="true" />
        <strong>Developer Bot</strong>
        <span className="developer-activity-status" role={active ? 'status' : undefined}>{label}</span>
      </div>
      {updates.length > 0 && <details className="message-activity">
        <summary>{updates.length} activity {updates.length === 1 ? 'update' : 'updates'}</summary>
        <ol className="developer-activity-timeline">
          {updates.map((text, index) => <li key={index}>{text}</li>)}
        </ol>
      </details>}
      {children}
      {status === 'done' && message.developer_has_changes && onReview &&
        <button type="button" className="delegated-chat-link" onClick={onReview}>Review changes</button>}
    </section>
  );
}
