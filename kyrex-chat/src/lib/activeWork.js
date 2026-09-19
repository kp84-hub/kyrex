// activeWork.js — the Kyrex Chat sidebar's active-work line.
//
// ONE concise, visually secondary line, shown beneath a conversation's title
// only while that conversation has a pending or running Bot task or
// Chief-of-Staff delegation.
//
// Every word is derived EXCLUSIVELY from durable state — never model output:
//   * a durable task        (queued → running → awaiting_approval → terminal),
//   * a durable delegation  (target Bot + the owner-typed task text),
//   * and the live SSE/Flux status relayed for that same task.
//
// Kept dependency-free (plain ES module) so Node verifies the derivation
// directly (tests/activeWork.test.mjs) without rendering React.

export const TERMINAL_ACTIVITY_STATUSES = Object.freeze([
  'done',
  'failed',
  'cancelled',
  'rejected',
]);

const ACTIVE_STATUSES = Object.freeze([
  'queued',
  'running',
  'awaiting_approval',
]);

/** True when a status is final (no further updates expected). */
export function isTerminalActivity(status) {
  return TERMINAL_ACTIVITY_STATUSES.includes(String(status || ''));
}

/** True when a status represents pending/running work worth a subtitle. */
export function isActiveActivity(status) {
  return ACTIVE_STATUSES.includes(String(status || ''));
}

// A fixed verb → present-participle table. This is a lookup, NOT generation:
// a task is described by conjugating its own first word, and a verb we do not
// know is left verbatim rather than invented.
const GERUNDS = Object.freeze({
  read: 'Reading',
  find: 'Finding',
  fetch: 'Fetching',
  get: 'Getting',
  list: 'Listing',
  check: 'Checking',
  send: 'Sending',
  post: 'Posting',
  update: 'Updating',
  create: 'Creating',
  add: 'Adding',
  delete: 'Deleting',
  remove: 'Removing',
  open: 'Opening',
  write: 'Writing',
  draft: 'Drafting',
  review: 'Reviewing',
  summarize: 'Summarizing',
  summarise: 'Summarising',
  book: 'Booking',
  schedule: 'Scheduling',
  search: 'Searching',
  look: 'Looking',
  gather: 'Gathering',
  collect: 'Collecting',
});

function clean(text) {
  return String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
}

function stripTrailingPunctuation(text) {
  return text.replace(/[.!?,;:]+$/g, '').trim();
}

/** "calendar-reader" → "Calendar Reader" (last-resort Bot label). */
export function humanizeBotId(id) {
  const raw = clean(id);
  if (!raw) return '';
  return raw
    .split(/[-_]+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

/** Display name for a Bot id: the registry name, else a humanized slug. */
export function botDisplayName(bots, id) {
  const wanted = clean(id);
  if (!wanted) return '';
  const list = Array.isArray(bots) ? bots : [];
  const found = list.find((b) => b && String(b.id) === wanted);
  const named = found ? clean(found.name || found.id) : '';
  return named || humanizeBotId(wanted);
}

/**
 * The object phrase of an owner-typed instruction, for "Delegating …".
 * A leading imperative verb is dropped ("Read this week's calendar" →
 * "this week's calendar"); anything else is kept verbatim.
 */
export function objectPhrase(text) {
  let t = stripTrailingPunctuation(clean(text));
  if (!t) return 'a task';
  const words = t.split(' ');
  if (GERUNDS[words[0].toLowerCase()] && words.length > 1) {
    t = words.slice(1).join(' ');
  }
  return t || 'a task';
}

/** "Read this week's calendar" → "Reading this week's calendar". */
export function gerundPhrase(text) {
  const t = stripTrailingPunctuation(clean(text));
  if (!t) return '';
  const words = t.split(' ');
  const g = GERUNDS[words[0].toLowerCase()];
  if (!g) return '';
  return [g, ...words.slice(1)].join(' ');
}

/**
 * Truncate to a SINGLE line at a word boundary (ellipsis when cut).
 * Guarantees: no newlines, and never longer than *max* characters.
 */
export function truncateLine(text, max = 40) {
  const one = clean(text);
  const limit = Number.isFinite(max) && max > 1 ? Math.floor(max) : 40;
  if (one.length <= limit) return one;
  const clipped = one.slice(0, limit - 1);
  const atSpace = clipped.lastIndexOf(' ');
  const body = atSpace > limit * 0.5 ? clipped.slice(0, atSpace) : clipped;
  return body.replace(/[\s.!?,;:]+$/g, '') + '…';
}

/**
 * Derive the one-line subtitle for an activity descriptor, or ``null`` when
 * there is nothing to show (no active work, or a terminal/unknown state).
 *
 * descriptor shape (from the durable conversation list or the live overlay):
 *   { kind: 'task' | 'delegation', status, text, target_bot_id, ... }
 */
export function activityLine(activity, options = {}) {
  if (!activity || typeof activity !== 'object') return null;
  const status = String(activity.status || '');
  if (!isActiveActivity(status)) return null; // terminal/unknown → nothing

  const bots = options.bots;
  const kind = String(
    activity.kind || (activity.target_bot_id ? 'delegation' : 'task')
  );

  if (kind === 'delegation') {
    // A freshly-created delegation is "Delegating …"; once it is picked up we
    // are waiting on the target Bot.
    if (status === 'queued') {
      return truncateLine(`Delegating ${objectPhrase(activity.text)}`, options.max);
    }
    const target = botDisplayName(bots, activity.target_bot_id);
    return truncateLine(
      target ? `Waiting for ${target}` : 'Waiting for the other Bot',
      options.max
    );
  }

  // An ordinary Bot task.
  if (status === 'awaiting_approval') {
    return truncateLine('Waiting for your approval', options.max);
  }
  const phrase = gerundPhrase(activity.text);
  if (phrase) return truncateLine(phrase, options.max);
  const verbatim = clean(activity.text);
  if (verbatim) return truncateLine(verbatim, options.max);
  return truncateLine('Working…', options.max);
}

/**
 * Build the { conversationId: line } map the sidebar renders.
 *
 * ``live`` (from the SSE/Flux overlay) takes precedence over the durable
 * descriptor on the conversation — a ``null`` live entry clears a stale
 * durable line when the work has settled.
 */
export function buildActivityLines(conversations, options = {}) {
  const list = Array.isArray(conversations) ? conversations : [];
  const live = options.live || {};
  const lines = {};
  for (const c of list) {
    if (!c || !c.conversation_id) continue;
    const cid = c.conversation_id;
    const hasLive = Object.prototype.hasOwnProperty.call(live, cid);
    const activity = hasLive ? live[cid] : c.activity;
    const line = activityLine(activity, options);
    if (line) lines[cid] = line;
  }
  return lines;
}

/**
 * The non-terminal activities worth following on the Flux stream: one entry
 * per conversation with an active task id. Drives the live subscription so
 * every open bot chat updates without polling.
 */
export function activeSubscriptions(conversations) {
  const list = Array.isArray(conversations) ? conversations : [];
  const out = [];
  for (const c of list) {
    const a = c && c.activity;
    if (a && isActiveActivity(a.status) && a.task_id) {
      out.push({
        conversationId: c.conversation_id,
        taskId: a.task_id,
        activity: a,
      });
    }
  }
  return out;
}
