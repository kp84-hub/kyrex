import React, { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

// Flatten a react-markdown node tree into plain text (for copy buttons).
function nodeText(node) {
  if (node == null) return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join('');
  if (node.props && node.props.children) return nodeText(node.props.children);
  return '';
}

// Fenced code block: language label + copy button, styled body.
function CodeBlock({ lang, className, text }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable — non-fatal */
    }
  };

  return (
    <div className="code-block">
      <div className="code-block-bar">
        <span className="code-block-lang">{lang || 'code'}</span>
        <button
          type="button"
          className="code-block-copy"
          onClick={copy}
          aria-label={copied ? 'Copied' : 'Copy code'}
        >
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre>
        <code className={className}>{text}</code>
      </pre>
    </div>
  );
}

const markdownComponents = {
  pre: ({ children }) => {
    const child = Array.isArray(children) ? children[0] : children;
    const className = child?.props?.className || '';
    const match = /language-([\w-]+)/.exec(className);
    const text = nodeText(child?.props?.children);
    return (
      <CodeBlock lang={match ? match[1] : ''} className={className} text={text} />
    );
  },
};

// Inline approval prompt for a Bot task's pending approval. T1 is y/n; T2
// requires the exact token (timeout otherwise denies). The reply is sent to
// the task-scoped respond endpoint, never to a global approval handler.
function ApprovalPrompt({ approval, onRespond }) {
  const [value, setValue] = useState('');

  if (!approval || !approval.task_id) return null;

  const send = (text) => {
    if (!text) return;
    onRespond(approval.task_id, text);
    setValue('');
  };

  return (
    <div className="approval-card" role="status">
      <div className="approval-summary">
        <span className="approval-badge">T{approval.tier}</span>
        <span>{approval.summary || 'Approval required'}</span>
        {approval.detail ? <span className="approval-detail">{approval.detail}</span> : null}
      </div>
      {approval.tier === 2 ? (
        <div className="approval-actions">
          <input
            className="approval-input"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') send(value.trim());
            }}
            placeholder="Type the exact token to approve"
            aria-label="Approval token"
          />
          <button
            type="button"
            className="approval-btn approve"
            disabled={!value.trim()}
            onClick={() => send(value.trim())}
          >
            Approve
          </button>
        </div>
      ) : (
        <div className="approval-actions">
          <button
            type="button"
            className="approval-btn approve"
            onClick={() => send('y')}
          >
            Approve (y)
          </button>
          <button
            type="button"
            className="approval-btn deny"
            onClick={() => send('n')}
          >
            Deny (n)
          </button>
        </div>
      )}
      <div className="approval-hint">Approvals are scoped to this task only.</div>
    </div>
  );
}

export default function Message({ message, onRetry, isLastAssistant, onRespondApproval }) {
  const isUser = message.role === 'user';

  return (
    <div className={`message message-${message.role}`}>
      <div className="message-body">
        {isUser ? (
          <div className="message-content message-bubble">{message.content}</div>
        ) : (
          <div className="message-content markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
              {message.content || ''}
            </ReactMarkdown>
          </div>
        )}
        {message.task && (
          <div className="message-task">
            <span className={`task-dot task-${message.task.status}`} aria-hidden="true" />
            <span>Task {message.task.status}</span>
          </div>
        )}
        {message.events && message.events.length > 0 && (
          <div className="message-events">
            {message.events.map((ev, i) => {
              if (ev.kind === 'progress') {
                const text = Object.entries(ev.payload || {})
                  .map(([k, v]) => `${k}: ${v}`)
                  .join(' · ');
                return (
                  <div key={i} className="event-line event-progress">{text}</div>
                );
              }
              if (ev.kind === 'approval_request') {
                return (
                  <div key={i} className="event-line event-approval">
                    Approval requested (T{ev.tier}): {ev.summary}
                  </div>
                );
              }
              if (ev.kind === 'approval_result') {
                return (
                  <div key={i} className="event-line event-approval-result">
                    → {ev.decision}
                  </div>
                );
              }
              return null;
            })}
          </div>
        )}
        {message.approval && onRespondApproval && (
          <ApprovalPrompt
            approval={message.approval}
            onRespond={(taskId, text) => onRespondApproval(taskId, text, message.id)}
          />
        )}
        {message.cancelled && (
          <div className="message-cancelled" role="status">
            Stopped — partial response kept
          </div>
        )}
        {message.error && <div className="message-error">{message.error}</div>}
        {message.error && isLastAssistant && !message.streaming && onRetry && (
          <button type="button" className="retry-btn" onClick={onRetry}>
            Retry
          </button>
        )}
        {message.streaming && !message.content && (
          <span className="typing-dots" aria-label="Kyrex is responding">
            <span /><span /><span />
          </span>
        )}
      </div>
    </div>
  );
}
