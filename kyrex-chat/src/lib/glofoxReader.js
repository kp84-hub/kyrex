// glofoxReader.js — the deterministic decisions behind the read-only Glofox
// Reader preset in the Bot Settings surface.
//
// Proves, WITHOUT rendering React and WITHOUT any network:
//   1. the badge is derived EXCLUSIVELY from the server's own `glofox_reader`
//      flag — the UI never re-derives it from a policy or a local guess;
//   2. the preset's only granted operation is the pinned `glofox:read`
//      schedule read — no browser, write, delete, push, mail, calendar, or
//      coordination capability;
//   3. the ONE owner-facing command is the byte-exact `glofox: schedule`.

// The only host operation a Glofox Reader is granted. Kept here so the
// confirmation copy can name it without re-encoding the backend policy; the
// authoritative grant is the preset the server returns.
export const GLOFOX_READER_ALLOWED_OPS = ['glofox:read'];

// The badge label shown next to a Bot the server reports as a Glofox Reader.
export const GLOFOX_READER_BADGE_LABEL = 'Glofox Reader';

// The preset id the create form submits — byte-identical to the backend's
// GLOFOX_READER_PRESET_ID so the existing create/configure endpoints pick it
// up without any translation layer.
export const GLOFOX_READER_PRESET_ID = 'glofox-reader';

// The ONE owner-facing command a Glofox Reader serves. Byte-exact: the server
// routes ONLY this exact text to the glofox executor (nothing is guessed,
// compiled, or forwarded).
export const GLOFOX_READER_COMMAND = 'glofox: schedule';

// The badge is a PURE function of server state. `bot.glofox_reader` is computed
// server-side (EXACTLY the least-privilege glofox:read grant, with no other
// capability); the UI must never recompute it from a policy or a local guess.
export function glofoxReaderBadge(bot) {
  return Boolean(bot && bot.glofox_reader === true);
}

// The confirmation's effective-permission rows for a preset, as [op, tier]
// pairs sorted by operation. Values come straight from the server
// (serve.effective_permissions) — the UI only shapes them for display.
export function glofoxReaderPermissionRows(preset) {
  const perms = (preset && preset.permissions) || {};
  return Object.entries(perms).sort(([a], [b]) => a.localeCompare(b));
}

// The requirements the server enforces before the glofox-reader preset is
// enabled, expressed from what the UI knows — the EXACT mirrors of the
// configure endpoint's 409 gates so the confirmation can explain WHY the
// action is unavailable. The server remains authoritative and re-checks both.
// A Glofox Reader has NO browser surface: a non-empty browser domain
// allowlist OR an explicit Browser Host binding is a blocker.
export function glofoxReaderBlockers(bot, { boundHostId = '' } = {}) {
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
export function canConfigureGlofoxReader(bot, opts) {
  return glofoxReaderBlockers(bot, opts).length === 0;
}

// The Bot is configurable as a Glofox Reader only WITH the preset present (the
// server exposes the preset) — but a Bot with NO provider profile can hold the
// grant and still never serve a turn. This is a WARN in the confirmation, not
// a blocker: configuring the policy does not require an LLM.
export function glofoxReaderNeedsProvider(bot) {
  return !(bot && bot.provider_profile_id);
}
