// calendarReader.js — deterministic decisions for the Calendar Reader UI.
//
// Pure, dependency-free helpers kept out of the components so they can be
// verified without React or network:
//   * the byte-exact command vocabulary and its verdict;
//   * the Calendar Reader preset identity (exactly ``cal:list`` at tier 0);
//   * the badge, derived EXCLUSIVELY from the server's own `calendar_reader`
//     roster flag (never re-derived from a policy or a local guess);
//   * the effective-permission rows and the server's 409 blockers.

// The only host operation a Calendar Reader is granted. Kept here so the
// confirmation copy can name it without re-encoding the backend policy; the
// authoritative grant is the preset the server returns.
export const CALENDAR_READER_ALLOWED_OPS = ["cal:list"];

export const CALENDAR_READER_PRESET_ID = "calendar-reader";
export const CALENDAR_READER_LABEL = "Calendar Reader";
export const CALENDAR_READER_BADGE_LABEL = "Calendar Reader";
export const CALENDAR_READER_POLICY = Object.freeze({ "cal:list": 0 });

export const CALENDAR_COMMANDS = Object.freeze([
  "calendar: today",
  "calendar: tomorrow",
  "calendar: week",
]);

export const CALENDAR_USAGE =
  "Try: calendar: today, calendar: tomorrow, or calendar: week";

// Exactly {cal:list: 0} and nothing else. A pure predicate, used by the preset
// option UI so it can only ever under-claim; the BADGE itself is server-derived
// from the roster flag, never from a policy shape.
export function isCalendarReaderPolicy(policy) {
  if (!policy || typeof policy !== "object" || Array.isArray(policy)) return false;
  const keys = Object.keys(policy);
  return keys.length === 1 && keys[0] === "cal:list" && policy["cal:list"] === 0;
}

export function isCalendarCommand(text) {
  return CALENDAR_COMMANDS.includes(String(text == null ? "" : text).trim());
}

// A message in the reserved namespace that is NOT one of the exact commands.
export function isCalendarNamespace(text) {
  return String(text == null ? "" : text).trim().toLowerCase().startsWith("calendar:");
}

// "command" | "unsupported" | "text" — the UI uses this to explain a
// fail-closed calendar message instead of pretending it was answered.
export function commandVerdict(text) {
  const t = String(text == null ? "" : text).trim();
  if (CALENDAR_COMMANDS.includes(t)) return "command";
  if (isCalendarNamespace(t)) return "unsupported";
  return "text";
}

// The badge is a PURE function of server state. `bot.calendar_reader` is
// computed server-side (EXACTLY the least-privilege cal:list grant, with no
// other capability); the UI must never recompute it from a policy or a local
// guess, and a non-boolean server value is not trusted.
export function calendarReaderBadge(bot) {
  return Boolean(bot && bot.calendar_reader === true);
}

// The confirmation's effective-permission rows for a preset, as [op, tier]
// pairs sorted by operation. Values come straight from the server
// (serve.effective_permissions) — the UI only shapes them for display.
export function calendarReaderPermissionRows(preset) {
  const perms = (preset && preset.permissions) || {};
  return Object.entries(perms).sort(([a], [b]) => a.localeCompare(b));
}

// The requirements the server enforces before the calendar-reader preset is
// enabled, expressed from what the UI knows — the EXACT mirrors of the
// configure endpoint's 409 gates so the confirmation can explain WHY the
// action is unavailable. The server remains authoritative and re-checks both.
// A Calendar Reader has NO browser surface: a non-empty browser domain
// allowlist OR an explicit Browser Host binding is a blocker.
export function calendarReaderBlockers(bot, { boundHostId = '' } = {}) {
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
export function canConfigureCalendarReader(bot, opts) {
  return calendarReaderBlockers(bot, opts).length === 0;
}

// A Bot with NO provider profile can hold the grant and still never serve a
// turn. This is a WARN in the confirmation, not a blocker: configuring the
// policy does not require an LLM.
export function calendarReaderNeedsProvider(bot) {
  return !(bot && bot.provider_profile_id);
}
