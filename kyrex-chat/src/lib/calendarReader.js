// calendarReader.js — deterministic decisions for the Calendar Reader UI.
//
// Pure, dependency-free helpers kept out of the components so they can be
// verified without React or network:
//   * the byte-exact command vocabulary and its verdict;
//   * the Calendar Reader preset identity (exactly ``cal:list`` at tier 0);
//   * a badge for a Bot that is (or is not) a Calendar Reader.

export const CALENDAR_READER_PRESET_ID = "calendar-reader";
export const CALENDAR_READER_LABEL = "Calendar Reader";
export const CALENDAR_READER_POLICY = Object.freeze({ "cal:list": 0 });

export const CALENDAR_COMMANDS = Object.freeze([
  "calendar: today",
  "calendar: tomorrow",
  "calendar: week",
]);

export const CALENDAR_USAGE =
  "Try: calendar: today, calendar: tomorrow, or calendar: week";

// Exactly {cal:list: 0} and nothing else. Used by the preset option UI so the
// badge can only ever under-claim.
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

export function calendarReaderBadge(bot) {
  return isCalendarReaderPolicy(bot && bot.policy)
    ? { label: CALENDAR_READER_LABEL, tone: "ok" }
    : null;
}
