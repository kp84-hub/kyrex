import React, { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { developerWorkspace } from '../lib/api.js';
import { sanitizeAssistantText } from '../lib/sanitize.js';

function FileDiff({ botId, revision, path, error, onRefresh }) {
  const [requested, setRequested] = useState(false);
  const [patch, setPatch] = useState(null);
  const [failure, setFailure] = useState('');
  useEffect(() => {
    if (!requested || error) return;
    const controller = new AbortController();
    developerWorkspace(botId, { file: path, revision, signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setPatch(result); })
      .catch(e => { if (!controller.signal.aborted) setFailure(e.message); });
    return () => controller.abort();
  }, [requested, error, botId, revision, path]);

  return <details className="developer-file-diff" onToggle={event => {
    if (event.currentTarget.open) setRequested(true);
  }}>
    <summary>{path}</summary>
    {requested && (error || patch?.error ? <p>{error || patch.error}</p>
      : failure ? <>
        <p role="alert">{failure}</p>
        <button type="button" className="bot-configure-btn" onClick={onRefresh}>Refresh files</button>
      </> : !patch ? <p role="status">Loading file diff…</p>
      : typeof patch.diff === 'string' ? <pre tabIndex={0} aria-label={`Diff for ${path}`}><code>{patch.diff}</code></pre>
      : <p>File diff is unavailable.</p>)}
  </details>;
}

export default function DeveloperChanges({ botId, report, onClose, onRepositorySettings }) {
  const dialog = useRef(null);
  const [review, setReview] = useState(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const element = dialog.current;
    element.showModal();
    return () => { element.close(); };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    developerWorkspace(botId, { review: true, signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setReview(result); })
      .catch(e => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [botId, attempt]);

  function refreshFiles() {
    setError('');
    setReview(null);
    setAttempt(value => value + 1);
  }

  const taskReport = sanitizeAssistantText(report || '');
  return <dialog className="developer-changes" ref={dialog} onCancel={event => {
    event.preventDefault(); onClose();
  }} aria-labelledby="developer-changes-title">
    <header className="developer-changes-header">
      <h2 id="developer-changes-title">Review changes</h2>
      <button type="button" className="bot-configure-btn" onClick={onClose}>Close</button>
    </header>
    <div className="developer-changes-body">
      {error ? <>
        <p role="alert">{error}</p>
        <button type="button" className="bot-configure-btn" onClick={refreshFiles}>Try again</button>
      </> : !review ? <p role="status">Loading workspace changes…</p> : <>
        <p className="developer-changes-context">{review.repository || 'Original workspace'}
          {review.branch && <> · <code>{review.branch}</code></>}</p>
        {!review.review_available ? <p>No Git checkout is available to review.</p> : <>
          <p className="developer-changes-context">Current workspace compared with {review.base ? `the saved ${review.base} base` : 'HEAD'}.
            {' '}This view can include changes from earlier runs.</p>
          {review.changes.length ? <>
            <h3>{review.changes.length} changed {review.changes.length === 1 ? 'file' : 'files'}</h3>
            {review.changes.map(({ path, error }) => <FileDiff key={path} botId={botId} revision={review.revision}
              path={path} error={error} onRefresh={refreshFiles} />)}
          </> : <p>No workspace changes.</p>}
        </>}
      </>}
      {taskReport && <details className="developer-file-diff">
        <summary>Task report and test results</summary>
        <div className="message-content markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{taskReport}</ReactMarkdown></div>
      </details>}
    </div>
    {review?.base && <footer className="developer-changes-footer">
      <button type="button" className="bot-configure-btn" onClick={onRepositorySettings}>Repository settings</button>
    </footer>}
  </dialog>;
}
