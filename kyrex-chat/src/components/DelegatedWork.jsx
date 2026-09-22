import React, { useEffect, useState } from 'react';
import {
  delegationApprovalOf,
  readDismissedDelegations,
  writeDismissedDelegations,
  withDismissedDelegation,
  visibleDelegations,
} from '../lib/delegations.js';

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

export default function DelegatedWork({
  delegations,
  conversationId,
  onRespondApproval,
}) {
  const rows = Array.isArray(delegations) ? delegations : [];
  const [busyTask, setBusyTask] = useState('');
  const [tokens, setTokens] = useState({});
  const [error, setError] = useState('');
  // Completed-work acknowledgements are persisted per conversation, so a
  // dismissal survives a browser refresh instead of resetting with the mount.
  const [dismissed, setDismissed] = useState(() =>
    readDismissedDelegations(conversationId)
  );

  // Each conversation keeps its own acknowledgement set: switching the active
  // conversation re-reads the store under THAT conversation's key.
  useEffect(() => {
    setDismissed(readDismissedDelegations(conversationId));
  }, [conversationId]);

  // Acknowledge a completed row and persist it. Only done rows are ever
  // recorded (withDismissedDelegation enforces that), so live work can never
  // be hidden by acknowledgement.
  const dismiss = (delegation) => {
    const next = withDismissedDelegation(dismissed, delegation);
    if (next.size === dismissed.size) return;
    writeDismissedDelegations(conversationId, next);
    setDismissed(next);
  };

  const visibleRows = visibleDelegations(rows, dismissed);
  if (visibleRows.length === 0) return null;

  const respond = async (approval, text) => {
    if (!approval || !text || !onRespondApproval || busyTask) return;
    setBusyTask(approval.task_id);
    setError('');
    try {
      await onRespondApproval(approval.task_id, text);
    } catch (e) {
      setError(String((e && e.message) || 'Could not record approval'));
    } finally {
      setBusyTask('');
    }
  };

  return (
    <section className="delegated-work" aria-label="Delegated work">
      <div className="delegated-work-title">Delegated work</div>
      <ul className="delegated-work-list">
        {visibleRows.map((d) => {
          const status = String(d.status || 'unknown');
          const label = STATUS_LABEL[status] || status;
          const approval = delegationApprovalOf(d);
          const token = approval ? (tokens[approval.task_id] || '') : '';
          return (
            <li key={d.delegation_id} className="delegated-work-item">
              <div className="delegated-work-head">
                <span className="delegated-work-target">{targetName(d)}</span>
                {status === 'done' ? (
                  <button
                    type="button"
                    className={`delegated-work-status delegated-work-dismiss status-${status}`}
                    aria-label={`Dismiss completed ${targetName(d)} delegation`}
                    onClick={() => dismiss(d)}
                  >{label}</button>
                ) : (
                  <span className={`delegated-work-status status-${status}`}>
                    {label}
                  </span>
                )}
              </div>
              {d.text ? <div className="delegated-work-task">{d.text}</div> : null}
              {approval && onRespondApproval ? (
                <div className="approval-card" role="status">
                  <div className="approval-summary">
                    <span className="approval-badge">T{approval.tier}</span>
                    <span>{approval.summary || 'Approval required'}</span>
                    {approval.detail
                      ? <span className="approval-detail">{approval.detail}</span>
                      : null}
                  </div>
                  {approval.tier === 2 ? (
                    <div className="approval-actions">
                      <input
                        className="approval-input"
                        value={token}
                        onChange={(e) => setTokens((old) => ({
                          ...old, [approval.task_id]: e.target.value,
                        }))}
                        placeholder="Type the exact token to approve"
                        aria-label="Delegated approval token"
                      />
                      <button
                        type="button"
                        className="approval-btn approve"
                        disabled={busyTask === approval.task_id || !token.trim()}
                        onClick={() => respond(approval, token.trim())}
                      >Approve</button>
                    </div>
                  ) : (
                    <div className="approval-actions">
                      <button
                        type="button"
                        className="approval-btn approve"
                        disabled={busyTask === approval.task_id}
                        onClick={() => respond(approval, 'y')}
                      >Approve (y)</button>
                      <button
                        type="button"
                        className="approval-btn deny"
                        disabled={busyTask === approval.task_id}
                        onClick={() => respond(approval, 'n')}
                      >Deny (n)</button>
                    </div>
                  )}
                  <div className="approval-hint">
                    Your reply is scoped to this delegated task only.
                  </div>
                </div>
              ) : null}
              {d.result_summary
                ? <div className="delegated-work-summary">{d.result_summary}</div>
                : null}
              {(status === 'rejected' || status === 'failed' || status === 'cancelled')
                && d.error
                ? <div className="delegated-work-error">{d.error}</div>
                : null}
            </li>
          );
        })}
      </ul>
      {error ? <div className="delegated-work-error">{error}</div> : null}
    </section>
  );
}
