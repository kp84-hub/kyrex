import React, { useEffect, useRef, useState } from 'react';
import Message from './Message.jsx';

const STICK_THRESHOLD_PX = 96;

// Intelligent auto-scroll: follow the stream only while the user is near the
// bottom; never yank the viewport when they scroll up to read. A "Latest"
// pill appears when detached so they can re-attach with one click.
export default function MessageList({ messages, isGenerating, onRetry, onRespondApproval }) {
  const containerRef = useRef(null);
  const scrollFrameRef = useRef(null);
  const autoScrollingRef = useRef(false);
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

  const onScroll = () => {
    if (!autoScrollingRef.current) evaluate();
  };

  const detachFromStream = () => {
    autoScrollingRef.current = false;
    stickRef.current = false;
    setDetached(true);
    if (scrollFrameRef.current != null) {
      cancelAnimationFrame(scrollFrameRef.current);
      scrollFrameRef.current = null;
    }
  };

  useEffect(() => {
    const el = containerRef.current;
    if (el && stickRef.current) {
      if (scrollFrameRef.current != null) return;
      autoScrollingRef.current = true;
      const follow = () => {
        scrollFrameRef.current = null;
        if (!stickRef.current || !containerRef.current) return;
        const node = containerRef.current;
        const target = node.scrollHeight - node.clientHeight;
        const distance = target - node.scrollTop;
        if (Math.abs(distance) < 1) {
          node.scrollTop = target;
          autoScrollingRef.current = false;
          return;
        }
        node.scrollTop += distance * 0.28;
        scrollFrameRef.current = requestAnimationFrame(follow);
      };
      scrollFrameRef.current = requestAnimationFrame(follow);
    }
  }, [messages]);

  useEffect(() => () => {
    if (scrollFrameRef.current != null) {
      cancelAnimationFrame(scrollFrameRef.current);
    }
  }, []);

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
        onWheel={detachFromStream}
        onTouchMove={detachFromStream}
        aria-live="polite"
        aria-relevant="additions text"
      >
        {messages.map((m, i) => (
          <Message
            key={m.id}
            message={m}
            isLastAssistant={m.role === 'assistant' && i === messages.length - 1}
            onRetry={onRetry}
            onRespondApproval={onRespondApproval}
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
