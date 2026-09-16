// browserBot.js — the deterministic decisions behind the read-only Browser Bot
// preset in the Bot Settings surface.
//
// Proves, WITHOUT rendering React and WITHOUT any network:
//   1. the badge is derived EXCLUSIVELY from the server's own `browser_bot`
//      flag — the UI never re-derives it from the allowlist or the policy;
//   2. the confirmation's entitlement preview lists the browser preset's
//      effective, host-derived permissions verbatim (the two read-only
//      operations the preset grants, everything else denied);
//   3. the "can I enable it?" gate matches the server's rules: a non-empty
//      browser allowlist AND an explicit Browser Host binding.

// The only browser operations a Browser Bot is allowed to perform. Kept here
// so the confirmation copy can name them without re-encoding the backend
// policy; the authoritative grant is the preset the server returns.
export const BROWSER_BOT_ALLOWED_OPS = ['browser:navigate', 'browser:read'];

// The badge label shown next to a Bot the server reports as a Browser Bot.
export const BROWSER_BOT_BADGE_LABEL = 'Browser Bot';

// The badge is a PURE function of server state. `bot.browser_bot` is computed
// server-side (read-only grant + non-empty allowlist + explicit host binding);
// the UI must never recompute it from `browser_allowlist` or a local guess.
export function browserBotBadge(bot) {
  return Boolean(bot && bot.browser_bot === true);
}

// The confirmation's effective-permission rows for a preset, as [op, tier]
// pairs sorted by operation. Values come straight from the server
// (serve.effective_permissions) — the UI only shapes them for display.
export function browserBotPermissionRows(preset) {
  const perms = (preset && preset.permissions) || {};
  return Object.entries(perms).sort(([a], [b]) => a.localeCompare(b));
}

// The requirements the server enforces before the browser preset is enabled.
// Mirrors the configure endpoint's fail-closed gate so the UI can explain WHY
// the action is unavailable — the server remains authoritative.
export function browserBotBlockers(bot, { boundHostId = '' } = {}) {
  const blockers = [];
  const list = (bot && bot.browser_allowlist) || [];
  if (!Array.isArray(list) || list.length === 0) {
    blockers.push('a non-empty browser domain allowlist');
  }
  if (!boundHostId) {
    blockers.push('an explicit Browser Host binding');
  }
  return blockers;
}

// True only when BOTH server-side requirements are met from what the UI knows.
// Used to disable the confirm action and to explain the blockers; the server
// re-checks everything on the actual request.
export function canEnableBrowserBot(bot, host) {
  return browserBotBlockers(bot, host).length === 0;
}
