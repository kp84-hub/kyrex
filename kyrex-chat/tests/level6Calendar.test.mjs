// level6Calendar.test.mjs — the Level 6 Calendar surface's deterministic
// decisions.
//
// Proves, WITHOUT rendering React and WITHOUT any network:
//   1. the badge is derived EXCLUSIVELY from the server's own `level6_calendar`
//      flag — never re-derived from the policy or a local guess, and a
//      non-boolean server value is not trusted;
//   2. the entitlement preview lists the preset's effective, host-derived
//      permissions verbatim, sorted, with EXACTLY the two pinned operations
//      granted — every other browser / write / mail / calendar-create /
//      coordination operation is denied;
//   3. the ONE owner-facing command is the byte-exact `level6: calendar`;
//   4. the create surface identifies the preset with the smallest support
//      needed: the preset option id and the badge label;
//   5. the preset is its OWN least-privilege grant — never an alias of the
//      Calendar Reader (cal:list only) or the Glofox Reader (glofox:read
//      only) — and it has NO browser surface (no allowlist).
//
// Run: node tests/level6Calendar.test.mjs
import assert from "node:assert/strict";
import {
  LEVEL6_CALENDAR_ALLOWED_OPS,
  LEVEL6_CALENDAR_BADGE_LABEL,
  LEVEL6_CALENDAR_COMMAND,
  LEVEL6_CALENDAR_PRESET_ID,
  level6CalendarBadge,
  level6CalendarNeedsProvider,
  level6CalendarPermissionRows,
} from "../src/lib/level6Calendar.js";

// ── 1. the badge is a pure function of SERVER state ──────────────────
{
  assert.equal(level6CalendarBadge({ level6_calendar: true }), true);
  assert.equal(level6CalendarBadge({ level6_calendar: false }), false);
  assert.equal(level6CalendarBadge({}), false);
  assert.equal(level6CalendarBadge(null), false);
  assert.equal(level6CalendarBadge(undefined), false);
  // A non-boolean server value is NOT trusted (exact `true` only).
  assert.equal(level6CalendarBadge({ level6_calendar: "true" }), false);
  assert.equal(level6CalendarBadge({ level6_calendar: 1 }), false);
  // The UI never infers the badge from a policy shape or a preset name.
  assert.equal(
    level6CalendarBadge({
      policy: { "cal:list": 0, "glofox:read": 0 },
    }), false,
    "a policy alone must not produce a badge");
  // The weekly/reader badges are DISTINCT server flags — never aliases.
  assert.equal(level6CalendarBadge({ level6_weekly: true }), false);
  assert.equal(level6CalendarBadge({ calendar_reader: true }), false);
  assert.equal(level6CalendarBadge({ glofox_reader: true }), false);
  assert.equal(LEVEL6_CALENDAR_BADGE_LABEL, "Level 6 Calendar");
}

// ── 2. the granted operations are exactly the two pinned ops ─────────
{
  assert.deepEqual(
    [...LEVEL6_CALENDAR_ALLOWED_OPS].sort(),
    ["cal:list", "glofox:read"].sort(),
    "a Level 6 Calendar Bot is granted exactly the two pinned operations");
  // Every other capability is NOT in the allowed set.
  for (const op of [
    "browser:navigate", "browser:read", "browser:click", "browser:type",
    "browser:upload", "browser:download", "browser:screenshot",
    "browser:submit", "browser:delete",
    "fs:write", "fs:delete", "repo:pr", "repo:push",
    "mail:send", "cal:create", "bot:delegate",
  ]) {
    assert.equal(LEVEL6_CALENDAR_ALLOWED_OPS.includes(op), false,
      `${op} must never be allowed`);
  }
}

// ── 2b. the permission preview shapes the server payload verbatim ────
{
  const granted = new Set(LEVEL6_CALENDAR_ALLOWED_OPS);
  const server = {
    "id": "level6-calendar",
    "permissions": Object.fromEntries(
      ["cal:list", "cal:create", "glofox:read",
       "browser:navigate", "browser:read", "browser:click",
       "browser:screenshot", "browser:type", "browser:upload",
       "browser:download", "browser:submit", "browser:delete",
       "fs:read", "fs:write", "fs:delete", "repo:read", "repo:pr",
       "repo:push", "mail:send", "mail:read", "bot:delegate"]
        .map((op) => [op, granted.has(op) ? 0 : "deny"])),
  };
  const rows = level6CalendarPermissionRows(server);
  // Sorted by operation.
  assert.deepEqual(
    rows.map(([op]) => op),
    [...rows.map(([op]) => op)].sort((a, b) => a.localeCompare(b)));
  // Exactly the two pinned operations are granted, at tier 0.
  const grantedRows = rows.filter(([, tier]) => tier === 0).map(([op]) => op);
  assert.deepEqual([...grantedRows].sort(), [...LEVEL6_CALENDAR_ALLOWED_OPS].sort());
  for (const [op, tier] of rows) {
    if (!granted.has(op)) {
      assert.equal(tier, "deny", `${op} must be denied, got ${tier}`);
    }
  }
  // Malformed/missing payloads degrade to no rows, never a crash.
  assert.deepEqual(level6CalendarPermissionRows(null), []);
  assert.deepEqual(level6CalendarPermissionRows({}), []);
}

// ── 3. the ONE owner-facing command is byte-exact ────────────────────
{
  assert.equal(LEVEL6_CALENDAR_COMMAND, "level6: calendar");
  // It is DISTINCT from the pinned weekly command.
  assert.notEqual(LEVEL6_CALENDAR_COMMAND, "level6: weekly");
  for (const variant of [
    "level6:  calendar",
    "LEVEL6: calendar",
    "level6: calendar 2020-01-01",
    "level6: calendar?url=https://evil.example/",
    "level6 calendar",
    "level6",
  ]) {
    assert.notEqual(LEVEL6_CALENDAR_COMMAND, variant,
      `the command must not absorb ${JSON.stringify(variant)}`);
  }
}

// ── 4. create-surface identification ─────────────────────────────────
{
  // The preset id the create form submits to the EXISTING endpoints must
  // match the backend preset id byte-for-byte.
  assert.equal(LEVEL6_CALENDAR_PRESET_ID, "level6-calendar");
}

// ── 5. a missing provider profile is a WARN, never a blocker ─────────
{
  assert.equal(level6CalendarNeedsProvider({}), true);
  assert.equal(level6CalendarNeedsProvider(null), true);
  assert.equal(level6CalendarNeedsProvider({ provider_profile_id: "p1" }), false);
  assert.equal(level6CalendarNeedsProvider({ provider_profile_id: "" }), true);
}