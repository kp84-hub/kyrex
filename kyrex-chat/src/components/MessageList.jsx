import React, { useEffect, useRef, useState } from 'react';
import Message from './Message.jsx';

const STICK_THRESHOLD_PX = 96;

// Intelligent auto-scroll: follow the stream only while the user is near the
// bottom; never yank the viewport when they scroll up to read. A "Latest"
// pill appears when detached so they can re-attach with one click.
export default function MessageList({ messages, isGenerating, onRetry }) {
  const containerRef = useRef(null);
  const stickRef = useRef(true);
  const [detached, setDetached] = useState(false);

  const evaluate = () => {
    const el = containerRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const near = distance < STICK_THRESHOLD_PX;
    stickRef.current = near;
    setDetached(!near);
  };

  const onScroll = () => evaluate();

  useEffect(() => {
    const el = containerRef.current;
    if (el && stickRef.current) {
      // Instant jump while streaming (smooth scrolling lags token flow).
      el.scrollTop = el.scrollHeight;
    }
  }, [messages]);

  // When generation ends while the user is detached, leave the viewport alone.
  const jumpToLatest = () => {
    const el = containerRef.current;
    stickRef.current = true;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
    setDetached(false);
  };

  return (
    <div className="message-list-wrap">
      <div
        ref={containerRef}
        className="message-list"
        onScroll={onScroll}
        aria-live="polite"
        aria-relevant="additions text"
      >
        {messages.map((m, i) => (
          <Message
            key={m.id}
            message={m}
            isLastAssistant={m.role === 'assistant' && i === messages.length - 1}
            onRetry={onRetry}
          />
        ))}
      </div>
      {detached && (
        <button
          type="button"
          className="jump-latest"
          onClick={jumpToLatest}
          aria-label="Jump to latest message"
        >
          <span aria-hidden="true">↓</span> Latest
        </button>
      )}
    </div>
  );
}
