import React from 'react';

export default function EmptyState({ onStart }) {
  return (
    <div className="empty-state">
      <div className="empty-mark">K</div>
      <h1 className="empty-title">How can Kyrex help?</h1>
      <p className="empty-sub">
        Ask a question, get a real answer — streamed live from the Kyrex engine.
      </p>
      <button className="empty-start" onClick={onStart}>
        Start a conversation
      </button>
    </div>
  );
}
