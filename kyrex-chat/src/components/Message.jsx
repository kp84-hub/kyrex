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

export default function Message({ message, onRetry, isLastAssistant }) {
  const isUser = message.role === 'user';

  return (
    <div className={`message message-${message.role}`}>
      <div className="message-avatar" title={isUser ? 'You' : 'Kyrex'}>
        {isUser ? 'You' : 'K'}
      </div>
      <div className="message-body">
        <div className="message-role">{isUser ? 'You' : 'Kyrex'}</div>
        {isUser ? (
          <div className="message-content">{message.content}</div>
        ) : (
          <div className="message-content markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
              {message.content || ''}
            </ReactMarkdown>
          </div>
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
