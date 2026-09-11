import React from 'react';

export default function EmptyState({ onStart }) {
  return (
    <div className="empty-state">
      <div className="empty-mark">K</div>
      <h1 className="empty-title">How can Kyrex help?</h1>
      <p className="empty-sub">
        Ask a question, get a real answer — streamed live from the Kyrex engine.
      </p>
      <p className="empty-sub">
        Kyrex Chat can also coordinate your available Bots. Pick a Bot from the
        Bot picker above to start a Bot-bound conversation — that Bot then acts
        with its own identity and permissions.
      </p>
      <button className="empty-start" onClick={onStart}>
        Start a conversation
      </button>
    </div>
  );
}
