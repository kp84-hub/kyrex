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

/** Safe pending approval for owner controls, or null. */
export function delegationApprovalOf(row) {
  if (!row || row.status !== 'awaiting_approval') return null;
  const approval = row.approval;
  if (!approval || !approval.task_id) return null;
  const tier = Number(approval.tier);
  if (tier !== 1 && tier !== 2) return null;
  return {
    task_id: String(approval.task_id),
    tier,
    summary: String(approval.summary || ''),
    detail: String(approval.detail || ''),
  };
}

// ── Terminal-work dismissal (persisted per conversation) ───────────────
//
// A TERMINAL delegation is dismissible: once the owner acknowledges it, the
// row must stay gone across a browser refresh. The acknowledgement is a set of
// delegation IDs stored per conversation under one versioned key, so dismissing
// work in one conversation never hides it in another.
//
// The invariant "only terminal work is ever hidden" lives here: an ID is
// recorded only for a terminal row, and a stored ID only ever filters a row
// that is STILL terminal. Reading/writing is best-effort — a sandboxed, full,
// or unreadable localStorage must never break the card.

export const DELEGATED_WORK_DISMISSED_PREFIX =
  'kyrex:delegated-work-dismissed:v1:';

/** The per-conversation localStorage key for dismissed delegated work. */
export function delegatedWorkDismissKey(conversationId) {
  return `${DELEGATED_WORK_DISMISSED_PREFIX}${String(conversationId ?? '')}`;
}

/**
 * A delegation row is dismissible once it is TERMINAL.
 *
 * done / failed / cancelled / rejected all have nothing left to refresh, so the
 * owner may acknowledge and hide any of them. This reuses the single terminal
 * predicate above so the two definitions can never drift apart. Non-terminal
 * work (queued / running / awaiting_approval) is NEVER dismissible.
 */
export function isDismissibleDelegation(status) {
  return isTerminalDelegation(status);
}

/**
 * The best available Web Storage, or null when unavailable/unreadable.
 *
 * Reading `localStorage` can throw (opaque origin / sandboxed storage), so the
 * lookup is guarded; the card then degrades to a non-persistent dismissal
 * rather than crashing.
 */
export function defaultDelegatedWorkStorage() {
  try {
    const g = typeof globalThis !== 'undefined' ? globalThis : undefined;
    if (g && g.localStorage) return g.localStorage;
    if (g && g.window && g.window.localStorage) return g.window.localStorage;
  } catch {
    /* Opaque origin / blocked storage: treat as absent. */
  }
  return null;
}

/**
 * Read the dismissed delegation-id set for a conversation.
 *
 * Never throws. An absent key, malformed JSON, or a non-array payload all
 * yield an empty set; a missing/unavailable storage yields an empty set too.
 * Non-string or empty entries are dropped.
 */
export function readDismissedDelegations(conversationId, storage) {
  const store = storage || defaultDelegatedWorkStorage();
  if (!store || !conversationId) return new Set();
  try {
    const raw = store.getItem(delegatedWorkDismissKey(conversationId));
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((id) => typeof id === 'string' && id));
  } catch {
    return new Set();
  }
}

/** Persist the dismissed delegation-id set for a conversation (best-effort). */
export function writeDismissedDelegations(conversationId, ids, storage) {
  const store = storage || defaultDelegatedWorkStorage();
  if (!store || !conversationId) return;
  try {
    store.setItem(
      delegatedWorkDismissKey(conversationId),
      JSON.stringify([...ids])
    );
  } catch {
    /* Quota/availability failure: the dismissal simply won't survive. */
  }
}

/**
 * Add a row's id to a dismissed set — but ONLY when the row is terminal.
 *
 * Returns the SAME membership for a non-terminal or malformed row, so a
 * queued / running / awaiting_approval delegation can never be hidden by
 * acknowledgement.
 */
export function withDismissedDelegation(ids, delegation) {
  const next = new Set(ids || []);
  if (
    delegation &&
    typeof delegation.delegation_id === 'string' &&
    delegation.delegation_id &&
    isDismissibleDelegation(delegation.status)
  ) {
    next.add(delegation.delegation_id);
  }
  return next;
}

/**
 * The rows the card should render: every row except dismissed terminal work.
 *
 * A stored id only hides the row it names while that row is STILL terminal — a
 * delegation that somehow regressed to a non-terminal status reappears, so
 * dismissal can never mask live work.
 */
export function visibleDelegations(rows, dismissed) {
  const list = Array.isArray(rows) ? rows : [];
  if (
    !dismissed ||
    typeof dismissed.has !== 'function' ||
    dismissed.size === 0
  ) {
    return list;
  }
  return list.filter((d) => {
    if (!d || typeof d.delegation_id !== 'string') return true;
    if (!dismissed.has(d.delegation_id)) return true;
    return !isDismissibleDelegation(d.status);
  });
}
