// level6Weekly.js — the deterministic decisions behind the dedicated
// "Level 6 Weekly" preset in the Bot Settings surface.
//
// Proves, WITHOUT rendering React and WITHOUT any network:
//   1. the badge is derived EXCLUSIVELY from the server's own `level6_weekly`
//      flag — the UI never re-derives it from a policy or a local guess;
//   2. the preset's granted operations are EXACTLY the four the pinned
//      `level6: weekly` command performs — browser navigate/read/screenshot
//      and the pinned glofox:read — with no write/delete/push/mail/calendar/
//      coordination capability;
//   3. its browser domain allowlist is the FIXED pinned Facebook host;
//   4. the ONE owner-facing command is the byte-exact `level6: weekly`.

// The four host operations the pinned `level6: weekly` command performs (all
// tier 0). Kept here so the confirmation copy can name them without
// re-encoding the backend policy; the authoritative grant is the preset the
// server returns.
export const LEVEL6_WEEKLY_ALLOWED_OPS = [
  'browser:navigate',
  'browser:read',
  'browser:screenshot',
  'glofox:read',
];

// The badge label shown next to a Bot the server reports as a Level 6 Weekly Bot.
export const LEVEL6_WEEKLY_BADGE_LABEL = 'Level 6 Weekly';

// The preset id the create/configure surface submits — byte-identical to the
// backend's LEVEL6_WEEKLY_PRESET_ID so the existing endpoints pick it up
// without any translation layer.
export const LEVEL6_WEEKLY_PRESET_ID = 'level6-weekly';

// The ONE owner-facing command a Level 6 Weekly Bot serves. Byte-exact: the
// server routes ONLY this exact text to the level6 executor (nothing is
// guessed, compiled, or forwarded).
export const LEVEL6_WEEKLY_COMMAND = 'level6: weekly';

// The preset's browser domain allowlist is FIXED to the pinned Level 6 Facebook
// host — the server stores exactly this list and a caller never supplies it.
export const LEVEL6_WEEKLY_ALLOWLIST = ['facebook.com'];

// The badge is a PURE function of server state. `bot.level6_weekly` is computed
// server-side (EXACTLY the dedicated four-operation grant, with no other
// capability); the UI must never recompute it from a policy or a local guess.
export function level6WeeklyBadge(bot) {
  return Boolean(bot && bot.level6_weekly === true);
}

// The confirmation's effective-permission rows for a preset, as [op, tier]
// pairs sorted by operation. Values come straight from the server
// (serve.effective_permissions) — the UI only shapes them for display.
export function level6WeeklyPermissionRows(preset) {
  const perms = (preset && preset.permissions) || {};
  return Object.entries(perms).sort(([a], [b]) => a.localeCompare(b));
}

// The preset's fixed browser domain allowlist (from the server payload when
// present, else the pinned constant). A caller never supplies it.
export function level6WeeklyAllowlist(preset) {
  const list = (preset && preset.browser_allowlist) || LEVEL6_WEEKLY_ALLOWLIST;
  return Array.isArray(list) ? [...list] : [];
}

// A Bot with NO provider profile can hold the grant and still never serve a
// turn. This is a WARN in the confirmation, not a blocker: configuring the
// policy does not require an LLM.
export function level6WeeklyNeedsProvider(bot) {
  return !(bot && bot.provider_profile_id);
}
