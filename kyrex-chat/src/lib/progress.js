import { safeText } from './connections.js';

const ACTIONS = {
  navigate: 'Opening the source…', read: 'Reading the source…',
  screenshot: 'Capturing the page…',
};

export function progressText(payload) {
  if (typeof payload?.stage === 'string' && payload.stage.trim()) {
    return safeText(payload.stage).trim().slice(0, 240);
  }
  return ACTIONS[payload?.action] || 'Working…';
}

export function progressUpdates(events = []) {
  const updates = [];
  for (const event of Array.isArray(events) ? events : []) {
    if (event?.kind !== 'progress') continue;
    const text = progressText(event.payload);
    if (updates.at(-1) !== text) updates.push(text);
  }
  return updates.slice(-100);
}

// Only a named stage may replace the sidebar's existing useful fallback.
export function latestProgressStage(events = []) {
  for (const event of (Array.isArray(events) ? events : []).slice().reverse()) {
    if (event?.kind === 'progress' && typeof event.payload?.stage === 'string'
      && event.payload.stage.trim()) return progressText(event.payload);
  }
  return '';
}

// Conversation snapshots and Flux can contain the same progress rows. Use
// the durable event IDs to replay the timeline without repeating old steps.
export function mergeDeveloperProgress(events, live) {
  const notes = new Map();
  for (const event of events) {
    if (event.kind === 'progress' && Number.isSafeInteger(event.payload?.event_id)) {
      notes.set(event.payload.event_id, event);
    }
  }
  for (const payload of live) notes.set(payload.event_id, { kind: 'progress', payload });
  return [...events.filter(event => event.kind !== 'progress' || !Number.isSafeInteger(event.payload?.event_id)),
    ...[...notes.entries()].sort(([a], [b]) => a - b).slice(-100).map(([, event]) => event)];
}
