// calendarWriter.js — deterministic decisions for the Calendar Writer UI.
//
// Pure, dependency-free helpers kept out of the components so they can be
// verified without React or network:
//   * the Calendar Writer preset identity (EXACTLY ``cal:create`` at tier 0);
//   * the WRITE-CAPABILITY badge, derived EXCLUSIVELY from the server's own
//     `calendar_writer` roster flag (never re-derived from a policy/guess);
//   * the effective-permission rows and the server's 409 blockers.

export const CALENDAR_WRITER_ALLOWED_OPS = ["cal:create"];

export const CALENDAR_WRITER_PRESET_ID = "calendar-writer";
export const CALENDAR_WRITER_LABEL = "Calendar Writer";
export const CALENDAR_WRITER_BADGE_LABEL = "Calendar Writer";
export const CALENDAR_WRITER_POLICY = Object.freeze({ "cal:create": 0 });

// The explicit, bounded request grammar the Calendar Writer accepts. Anything
// else (an ambiguous/missing time) is rejected before any task is created, and
// every create still passes the mandatory confirmation gate before the call.
export const CALENDAR_WRITER_GRAMMAR =
  "create <title> on YYYY-MM-DD from HH:MM to HH:MM";

// Exactly {cal:create: 0} and nothing else. A pure predicate, used by the preset
// option UI so it can only ever under-claim; the BADGE itself is server-derived.
export function isCalendarWriterPolicy(policy) {
  if (!policy || typeof policy !== "object" || Array.isArray(policy)) return false;
  const keys = Object.keys(policy);
  return keys.length === 1 && keys[0] === "cal:create"
    && policy["cal:create"] === 0;
}

// The badge is a PURE function of server state. `bot.calendar_writer` is
// computed server-side (EXACTLY the least-privilege cal:create grant, with no
// other capability); the UI must never recompute it from a policy or a local
// guess, and a non-boolean server value is not trusted.
export function calendarWriterBadge(bot) {
  return Boolean(bot && bot.calendar_writer === true);
}

// The confirmation's effective-permission rows for a preset, as [op, tier]
// pairs sorted by operation. Values come straight from the server
// (serve.effective_permissions) — the UI only shapes them for display.
export function calendarWriterPermissionRows(preset) {
  const perms = (preset && preset.permissions) || {};
  return Object.entries(perms).sort(([a], [b]) => a.localeCompare(b));
}

// The requirements the server enforces before the calendar-writer preset is
// enabled, expressed from what the UI knows — the EXACT mirrors of the
// configure endpoint's 409 gates. A Calendar Writer has NO browser surface: a
// non-empty browser domain allowlist OR an explicit Browser Host binding is a
// blocker. The server remains authoritative and re-checks both.
export function calendarWriterBlockers(bot, { boundHostId = '' } = {}) {
  const blockers = [];
  const list = (bot && bot.browser_allowlist) || [];
  if (Array.isArray(list)
      && list.some((h) => typeof h === 'string' && h.trim())) {
    blockers.push('a clean browser surface (remove the browser domain allowlist)');
  }
  if (boundHostId) {
    blockers.push('a clean browser surface (unbind the Browser Host)');
  }
  return blockers;
}

// True only when the server's 409 gates would pass, from what the UI knows.
// Used to disable the confirm action and explain WHY; the server re-checks.
export function canConfigureCalendarWriter(bot, opts) {
  return calendarWriterBlockers(bot, opts).length === 0;
}

// A Bot with NO provider profile can hold the grant and still never serve a
// turn. This is a WARN in the confirmation, not a blocker.
export function calendarWriterNeedsProvider(bot) {
  return !(bot && bot.provider_profile_id);
}
