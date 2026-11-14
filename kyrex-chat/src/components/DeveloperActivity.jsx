import React from 'react';
import { isActiveActivity } from '../lib/activeWork.js';
import { progressUpdates } from '../lib/progress.js';

const LABELS = {
  queued: 'Queued',
  running: 'Working in the repository…',
  awaiting_approval: 'Waiting for your approval',
  done: 'Reply ready',
  failed: 'Task failed',
  cancelled: 'Task cancelled',
  unknown: 'Checking task status…',
};

const CHECKS = { tests: 'Test command', build: 'Build command', lint: 'Lint command', diff: 'Git diff check' };
const OUTCOMES = { success: 'exited 0', failure: 'failed', timeout: 'timed out', unavailable: 'could not run' };
const CHECKOUT = {
  current: 'Matched the fetched base', updated: 'Updated to the fetched base',
  local_commits: 'Included commits ahead of the fetched base', behind: 'Was behind the fetched base',
  diverged: 'Differed from the fetched base', unverified: 'Freshness was unverified',
};

function Verification({ report }) {
  return <div className="developer-verification" aria-label="Verification recorded by Kyrex">
    <strong>What Kyrex checked</strong>
    {report.head && <p>Starting checkout <code>{report.head.slice(0, 12)}</code> · {CHECKOUT[report.freshness]}
      {report.remote_head && <span> (<code>{report.remote_head.slice(0, 12)}</code>)</span>}</p>}
    {report.checks.length ? <ul>
      {report.checks.map((check, index) => <li key={index}>
        {check.kind === 'github' ? <>
          <a href={check.url} target="_blank" rel="noreferrer">GitHub checks</a>
          {' · '}{Object.entries(check.counts).filter(([, count]) => count > 0)
            .map(([state, count]) => `${count} ${state}`).join(', ') || 'No results returned'}
          {check.partial && ' · Partial results'}
        </> : <>
          {check.runner ? <code>{check.runner}</code> : CHECKS[check.kind]} {OUTCOMES[check.outcome]}
          {check.outcome === 'failure' && check.exit_code != null && ` (exit ${check.exit_code})`}
        </>}
      </li>)}
    </ul> : <p>No test, build, lint, diff, or GitHub checks recorded for this reply.</p>}
  </div>;
}

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
      {message.developer_verification && <Verification report={message.developer_verification} />}
      {status === 'done' && message.developer_has_changes && onReview &&
        <button type="button" className="delegated-chat-link" onClick={onReview}>Review changes</button>}
    </section>
  );
}
