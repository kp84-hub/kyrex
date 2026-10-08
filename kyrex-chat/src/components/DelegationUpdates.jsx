import React from 'react';
import { latestProgressStage } from '../lib/progress.js';

// The Overwatcher's live relay uses the target's actual durable stage. One
// evolving line per task avoids turning tool activity into transcript spam.
export default function DelegationUpdates({ delegations = [] }) {
  const active = delegations.filter(d => d.target_conversation_id
    && ['queued', 'running', 'awaiting_approval'].includes(d.status));
  return active.map(d => {
    const stage = latestProgressStage((d.progress || []).map(payload => ({ kind: 'progress', payload })));
    const update = d.status === 'awaiting_approval' ? 'Waiting for your approval.'
      : d.status === 'queued' ? 'Queued.' : stage || 'Working on your task.';
    return <div key={d.delegation_id} className="message message-assistant overwatcher-progress">
      <div className="message-body" role="status" aria-live="polite">
        <span className="event-line">{d.target_bot_name || d.target_bot_id}: {update}</span>
      </div>
    </div>;
  });
}
