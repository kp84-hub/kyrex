import React, { useEffect, useState } from 'react';
import {
  readDismissedDelegations,
  writeDismissedDelegations,
  withDismissedDelegation,
  visibleDelegations,
  isDismissibleDelegation,
} from '../lib/delegations.js';

// DelegatedWork — a READ-ONLY status view of Bot-to-Bot delegated work.
//
// Status-only for this phase: it renders the coordinator's delegations (target
// Bot, status, timestamps, and the sanitized final summary) and offers NO
// approve/cancel controls. A delegated approval belongs to the TARGET task and
// is answered only by the owner through that task's existing flow — the
// coordinator cannot approve its own or another Bot's restricted action.
//
// A COMPLETED ("done") row carries one local control: "Done", which
// acknowledges the work so it stops appearing. That acknowledgement is owner
// state, not an approval — it is stored per conversation in localStorage and
// NEVER hides work that isn't finished.
//
// The rows are the backend's public_view shape and never contain secrets.
const STATUS_LABEL = {
  queued: 'Queued',
  running: 'Running',
  awaiting_approval: 'Awaiting approval',
  done: 'Done',
  failed: 'Failed',
  cancelled: 'Cancelled',
  rejected: 'Rejected',
};

function targetName(delegation) {
  return delegation.target_bot_id || 'target Bot';
}

export default function DelegatedWork({ delegations, conversationId }) {
  const rows = Array.isArray(delegations) ? delegations : [];
  const [dismissed, setDismissed] = useState(() =>
    readDismissedDelegations(conversationId)
  );

  // Each conversation keeps its own acknowledgement set: switching the active
  // conversation re-reads the store under THAT conversation's key rather than
  // carrying the previous chat's dismissals across.
  useEffect(() => {
    setDismissed(readDismissedDelegations(conversationId));
  }, [conversationId]);

  // Acknowledge a completed row and persist it. Non-done rows are never
  // added (withDismissedDelegation enforces that), so this is a no-op when the
  // row isn't finished.
  const dismiss = (delegation) => {
    const next = withDismissedDelegation(dismissed, delegation);
    if (next.size === dismissed.size) return;
    writeDismissedDelegations(conversationId, next);
    setDismissed(next);
  };

  const visible = visibleDelegations(rows, dismissed);
  if (visible.length === 0) return null;

  return (
    <section className="delegated-work" aria-label="Delegated work">
      <div className="delegated-work-title">Delegated work</div>
      <ul className="delegated-work-list">
        {visible.map((d) => {
          const status = String(d.status || 'unknown');
          const label = STATUS_LABEL[status] || status;
          return (
            <li key={d.delegation_id} className="delegated-work-item">
              <div className="delegated-work-head">
                <span className="delegated-work-target">
                  {targetName(d)}
                </span>
                <span className={`delegated-work-status status-${status}`}>
                  {label}
                </span>
                {isDismissibleDelegation(status) ? (
                  <button
                    type="button"
                    className="delegated-work-dismiss"
                    title="Dismiss this completed delegation"
                    aria-label={`Dismiss completed delegated work for ${targetName(d)}`}
                    onClick={() => dismiss(d)}
                  >
                    Done
                  </button>
                ) : null}
              </div>
              {d.text ? (
                <div className="delegated-work-task">{d.text}</div>
              ) : null}
              {d.result_summary ? (
                <div className="delegated-work-summary">{d.result_summary}</div>
              ) : null}
              {(status === 'rejected' || status === 'failed' || status === 'cancelled')
                && d.error ? (
                <div className="delegated-work-error">{d.error}</div>
              ) : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
