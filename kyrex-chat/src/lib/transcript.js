export const needsTaskRecovery = message => Boolean(message?.connection_interrupted || message?.task_recovery);

// Stored transcripts grow as turns and background results are appended. A
// focus/poll response can lag behind a completed stream; it must not remove
// messages already visible in the same conversation.
export function reconcileTranscript(current, incoming) {
  const byId = new Map(incoming.map(message => [message.id, message]));
  const visibleIds = new Map();
  const pending = [];
  for (const message of current) {
    let stored = byId.get(message.persisted_id || message.id) || byId.get(message.id);
    if (!stored && message.role === 'assistant' && message.turn_user_id && !needsTaskRecovery(message)) {
      // Bot tasks and specialized connector replies can persist under a
      // task/result id instead of the ordinary turn-assistant id. Match only
      // inside this request's turn, never against another identical reply.
      const start = incoming.findIndex(item => item.id === message.turn_user_id);
      if (start >= 0) {
        const end = incoming.findIndex((item, index) => index > start && item.role === 'user');
        stored = incoming.slice(start + 1, end < 0 ? undefined : end).find(item =>
          item.role === 'assistant' && item.content === message.content);
      }
    }
    if (!stored) {
      // A disconnected viewer still owns a durable task. Keep its bubble
      // until that task's saved result arrives, while allowing other results
      // to refresh around it. Never mistake a user-only snapshot for failure.
      if (needsTaskRecovery(message)) {
        pending.push(message);
        continue;
      }
      // Failed or stopped placeholders need not exist in durable storage.
      if (message.error || message.cancelled) continue;
      return current;
    }
    if (message.content && !stored.content && !message.error && !message.cancelled) return current;
    visibleIds.set(stored.id, message.id);
  }
  const reconciled = incoming.map(message => {
    const visibleId = visibleIds.get(message.id);
    return visibleId && visibleId !== message.id
      ? { ...message, id: visibleId, persisted_id: message.id }
      : message;
  });
  for (const message of pending) {
    const anchor = reconciled.findIndex(item => item.id === message.turn_user_id);
    if (anchor < 0) return current;
    const nextTurn = reconciled.findIndex((item, index) => index > anchor && item.role === 'user');
    reconciled.splice(nextTurn < 0 ? reconciled.length : nextTurn, 0, message);
  }
  return reconciled;
}
