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
