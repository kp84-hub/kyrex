// delegations.js — pure helpers for the Delegated Work card.
//
// Kept dependency-free so the bounded refresh decision can be verified
// directly (node tests/delegations.test.mjs) without rendering React.
//
// A delegation's lifecycle is the target task's lifecycle: queued -> running
// (-> awaiting_approval) -> done | failed | cancelled, plus rejected for a
// delegation refused before any task existed. Once a row is terminal the card
// has nothing left to refresh, so polling must STOP.

export const TERMINAL_DELEGATION_STATUSES = Object.freeze([
  'done',
  'failed',
  'cancelled',
  'rejected',
]);

/** True when a delegation status is final (no further updates expected). */
export function isTerminalDelegation(status) {
  return TERMINAL_DELEGATION_STATUSES.includes(String(status));
}

/**
 * Whether the card should keep refreshing.
 *
 * Returns true while at least one delegation is still non-terminal, and false
 * (stop polling) once every row is terminal — or when there are no delegations
 * at all. This is the single decision the UI's bounded poll loop consults, so
 * a finished conversation never spins an idle refresh loop.
 */
export function delegationsNeedPolling(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return false;
  // Only a row with a REAL status can keep the loop alive; a null/undefined
  // row (malformed payload) never drives an endless poll.
  return rows.some(
    (d) => d && d.status != null && !isTerminalDelegation(d.status)
  );
}
