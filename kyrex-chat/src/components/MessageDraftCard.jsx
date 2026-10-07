import React, { useState } from 'react';
import { preparePreviewMessage } from '../lib/api.js';
import MessageSendCard from './MessageSendCard.jsx';

export default function MessageDraftCard({ conversationId, message }) {
  const [sendId, setSendId] = useState(message.message_send?.id || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const prepare = async () => {
    if (busy) return;
    setBusy(true); setError('');
    try {
      const job = await preparePreviewMessage(conversationId, message.id);
      setSendId(job.id);
    } catch (e) {
      setError(e.message || 'Could not prepare this message.');
    } finally { setBusy(false); }
  };
  return <div aria-label="Workout message preview">
    {sendId ? <MessageSendCard key={sendId} id={sendId} onPrepareAgain={prepare} /> : <>
      <p>Ready to prepare for {message.message_draft.recipient}.</p>
      <button className="approval-btn" disabled={busy || !conversationId} onClick={prepare}>
        {busy ? 'Preparing…' : `Prepare for ${message.message_draft.recipient}`}
      </button>
    </>}
    {error ? <p className="message-error" role="alert">{error}</p> : null}
  </div>;
}
