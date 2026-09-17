// level6Weekly.test.mjs — the Level 6 Weekly surface's deterministic decisions.
//
// Proves, WITHOUT rendering React and WITHOUT any network:
//   1. the badge is derived EXCLUSIVELY from the server's own `level6_weekly`
//      flag — never re-derived from the policy or a local guess, and a
//      non-boolean server value is not trusted;
//   2. the entitlement preview lists the preset's effective, host-derived
//      permissions verbatim, sorted, with EXACTLY the four pinned operations
//      granted — every other browser / write / mail / calendar / coordination
//      operation is denied;
//   3. the preset's browser domain allowlist is the FIXED pinned Facebook host;
//   4. the ONE owner-facing command is the byte-exact `level6: weekly`;
//   5. the create surface identifies the preset with the smallest support
//      needed: the preset option id and the badge label.
//
// Run: node tests/level6Weekly.test.mjs
import assert from "node:assert/strict";
import {
  LEVEL6_WEEKLY_ALLOWED_OPS,
  LEVEL6_WEEKLY_ALLOWLIST,
  LEVEL6_WEEKLY_BADGE_LABEL,
  LEVEL6_WEEKLY_COMMAND,
  LEVEL6_WEEKLY_PRESET_ID,
  level6WeeklyAllowlist,
  level6WeeklyBadge,
  level6WeeklyNeedsProvider,
  level6WeeklyPermissionRows,
} from "../src/lib/level6Weekly.js";

// ── 1. the badge is a pure function of SERVER state ──────────────────
{
  assert.equal(level6WeeklyBadge({ level6_weekly: true }), true);
  assert.equal(level6WeeklyBadge({ level6_weekly: false }), false);
  assert.equal(level6WeeklyBadge({}), false);
  assert.equal(level6WeeklyBadge(null), false);
  assert.equal(level6WeeklyBadge(undefined), false);
  // A non-boolean server value is NOT trusted (exact `true` only).
  assert.equal(level6WeeklyBadge({ level6_weekly: "true" }), false);
  assert.equal(level6WeeklyBadge({ level6_weekly: 1 }), false);
  // The UI never infers the badge from a policy shape or a preset name.
  assert.equal(
    level6WeeklyBadge({
      policy: {
        "browser:navigate": 0, "browser:read": 0,
        "browser:screenshot": 0, "glofox:read": 0,
      },
    }), false,
    "a policy alone must not produce a badge");
  assert.equal(LEVEL6_WEEKLY_BADGE_LABEL, "Level 6 Weekly");
}

// ── 2. the granted operations are exactly the four pinned ops ─────────
{
  assert.deepEqual(
    [...LEVEL6_WEEKLY_ALLOWED_OPS].sort(),
    ["browser:navigate", "browser:read", "browser:screenshot", "glofox:read"]
      .sort(),
    "a Level 6 Weekly Bot is granted exactly the four pinned operations");
  // Every other capability is NOT in the allowed set.
  for (const op of [
    "browser:click", "browser:type", "browser:upload", "browser:download",
    "browser:submit", "browser:delete",
    "fs:write", "fs:delete", "repo:pr", "repo:push",
    "mail:send", "cal:create", "bot:delegate",
  ]) {
    assert.equal(LEVEL6_WEEKLY_ALLOWED_OPS.includes(op), false,
      `${op} must never be allowed`);
  }
}

// ── 2b. the permission preview shapes the server payload verbatim ────
{
  const granted = new Set(LEVEL6_WEEKLY_ALLOWED_OPS);
  const server = {
    "id": "level6-weekly",
    "browser_allowlist": ["facebook.com"],
    "permissions": Object.fromEntries(
      ["browser:navigate", "browser:read", "browser:click",
       "browser:screenshot", "browser:type", "browser:upload",
       "browser:download", "browser:submit", "browser:delete",
       "fs:read", "fs:write", "fs:delete", "repo:read", "repo:pr",
       "repo:push", "mail:send", "mail:read", "cal:read", "cal:create",
       "glofox:read", "bot:delegate"]
        .map((op) => [op, granted.has(op) ? 0 : "deny"])),
  };
  const rows = level6WeeklyPermissionRows(server);
  // Sorted by operation.
  assert.deepEqual(
    rows.map(([op]) => op),
    [...rows.map(([op]) => op)].sort((a, b) => a.localeCompare(b)));
  // Exactly the four pinned operations are granted, at tier 0.
  const grantedRows = rows.filter(([, tier]) => tier === 0).map(([op]) => op);
  assert.deepEqual([...grantedRows].sort(), [...LEVEL6_WEEKLY_ALLOWED_OPS].sort());
  for (const [op, tier] of rows) {
    if (!granted.has(op)) {
      assert.equal(tier, "deny", `${op} must be denied, got ${tier}`);
    }
  }
  // Malformed/missing payloads degrade to no rows, never a crash.
  assert.deepEqual(level6WeeklyPermissionRows(null), []);
  assert.deepEqual(level6WeeklyPermissionRows({}), []);
}

// ── 3. the fixed browser allowlist is the pinned Facebook host ───────
{
  assert.deepEqual(LEVEL6_WEEKLY_ALLOWLIST, ["facebook.com"]);
  // From the server payload when present; else the pinned constant.
  assert.deepEqual(level6WeeklyAllowlist({ browser_allowlist: ["facebook.com"] }),
    ["facebook.com"]);
  assert.deepEqual(level6WeeklyAllowlist(null), ["facebook.com"]);
  assert.deepEqual(level6WeeklyAllowlist({}), ["facebook.com"]);
  // A malformed payload degrades to an empty list (never a crash / guess).
  assert.deepEqual(level6WeeklyAllowlist({ browser_allowlist: "nope" }), []);
  // The returned copy never aliases the input or the constant.
  const src = ["facebook.com"];
  const out = level6WeeklyAllowlist({ browser_allowlist: src });
  out.push("evil.example");
  assert.deepEqual(src, ["facebook.com"]);
}

// ── 4. the ONE owner-facing command is byte-exact ────────────────────
{
  assert.equal(LEVEL6_WEEKLY_COMMAND, "level6: weekly");
  for (const variant of [
    "level6:  weekly",
    "LEVEL6: weekly",
    "level6: weekly tomorrow",
    "level6: weekly 2020-01-01",
    "level6: weekly?url=https://evil.example/",
    "level6 weekly",
    "level6",
  ]) {
    assert.notEqual(LEVEL6_WEEKLY_COMMAND, variant,
      `the command must not absorb ${JSON.stringify(variant)}`);
  }
}

// ── 5. create-surface identification ─────────────────────────────────
{
  // The preset id the create form submits to the EXISTING endpoints must match
  // the backend preset id byte-for-byte.
  assert.equal(LEVEL6_WEEKLY_PRESET_ID, "level6-weekly");
}

// ── 6. a missing provider profile is a WARN, never a blocker ─────────
{
  assert.equal(level6WeeklyNeedsProvider({}), true);
  assert.equal(level6WeeklyNeedsProvider(null), true);
  assert.equal(level6WeeklyNeedsProvider({ provider_profile_id: "p1" }), false);
  assert.equal(level6WeeklyNeedsProvider({ provider_profile_id: "" }), true);
}
