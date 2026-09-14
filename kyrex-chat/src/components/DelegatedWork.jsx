import React from 'react';

// DelegatedWork — a READ-ONLY status view of Bot-to-Bot delegated work.
//
// Status-only for this phase: it renders the coordinator's delegations (target
// Bot, status, timestamps, and the sanitized final summary) and offers NO
// approve/cancel controls. A delegated approval belongs to the TARGET task and
// is answered only by the owner through that task's existing flow — the
// coordinator cannot approve its own or another Bot's restricted action.
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

export default function DelegatedWork({ delegations }) {
  const rows = Array.isArray(delegations) ? delegations : [];
  if (rows.length === 0) return null;

  return (
    <section className="delegated-work" aria-label="Delegated work">
      <div className="delegated-work-title">Delegated work</div>
      <ul className="delegated-work-list">
        {rows.map((d) => {
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
              </div>
              {d.text ? (
                <div className="delegated-work-task">{d.text}</div>
              ) : null}
              {d.result_summary ? (
                <div className="delegated-work-summary">{d.result_summary}</div>
              ) : null}
              {status === 'rejected' && d.error ? (
                <div className="delegated-work-error">{d.error}</div>
              ) : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
