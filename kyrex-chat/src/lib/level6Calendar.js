// level6Calendar.js — the deterministic decisions behind the dedicated
// "Level 6 Calendar" preset in the Bot Settings surface.
//
// Proves, WITHOUT rendering React and WITHOUT any network:
//   1. the badge is derived EXCLUSIVELY from the server's own `level6_calendar`
//      flag — the UI never re-derives it from a policy or a local guess;
//   2. the preset's granted operations are EXACTLY the two the pinned
//      `level6: calendar` command performs — the owner's calendar read
//      (cal:list) and the pinned Glofox read (glofox:read) — with no
//      browser/write/delete/push/mail/coordination capability;
//   3. the ONE owner-facing command is the byte-exact `level6: calendar`;
//   4. the preset is its own least-privilege grant — never an alias of the
//      Calendar Reader (cal:list only) or the Glofox Reader (glofox:read
//      only), and neither of those presets is widened.

// The two host operations the pinned `level6: calendar` command performs (all
// tier 0). Kept here so the confirmation copy can name them without
// re-encoding the backend policy; the authoritative grant is the preset the
// server returns.
export const LEVEL6_CALENDAR_ALLOWED_OPS = [
  'cal:list',
  'glofox:read',
];

// The badge label shown next to a Bot the server reports as a Level 6 Calendar Bot.
export const LEVEL6_CALENDAR_BADGE_LABEL = 'Level 6 Calendar';

// The preset id the create/configure surface submits — byte-identical to the
// backend's LEVEL6_CALENDAR_PRESET_ID so the existing endpoints pick it up
// without any translation layer.
export const LEVEL6_CALENDAR_PRESET_ID = 'level6-calendar';

// The ONE owner-facing command a Level 6 Calendar Bot serves. Byte-exact: the
// server routes ONLY this exact text to the level6 executor (nothing is
// guessed, compiled, or forwarded).
export const LEVEL6_CALENDAR_COMMAND = 'level6: calendar';

// The badge is a PURE function of server state. `bot.level6_calendar` is
// computed server-side (EXACTLY the dedicated two-operation grant, with no
// other capability); the UI must never recompute it from a policy or a local
// guess.
export function level6CalendarBadge(bot) {
  return Boolean(bot && bot.level6_calendar === true);
}

// The confirmation's effective-permission rows for a preset, as [op, tier]
// pairs sorted by operation. Values come straight from the server
// (serve.effective_permissions) — the UI only shapes them for display.
export function level6CalendarPermissionRows(preset) {
  const perms = (preset && preset.permissions) || {};
  return Object.entries(perms).sort(([a], [b]) => a.localeCompare(b));
}

// A Bot with NO provider profile can hold the grant and still never serve a
// turn. This is a WARN in the confirmation, not a blocker: configuring the
// policy does not require an LLM.
export function level6CalendarNeedsProvider(bot) {
  return !(bot && bot.provider_profile_id);
}