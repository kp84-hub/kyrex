// glofoxReader.test.mjs — the Glofox Reader surface's deterministic decisions.
//
// Proves, WITHOUT rendering React and WITHOUT any network:
//   1. the badge is derived EXCLUSIVELY from the server's own `glofox_reader`
//      flag — never re-derived from the policy, an allowlist, or a local
//      guess, and a non-boolean server value is not trusted;
//   2. the entitlement preview lists the preset's effective, host-derived
//      permissions verbatim, sorted, with `glofox:read` the ONLY granted
//      operation — every browser / write / mail / calendar / coordination
//      operation is denied;
//   3. the ONE owner-facing command is the byte-exact `glofox: schedule`;
//   4. the create surface identifies the preset with the smallest support
//      needed: the preset option id and the badge label.
//
// Run: node tests/glofoxReader.test.mjs
import assert from "node:assert/strict";
import {
  canConfigureGlofoxReader,
  GLOFOX_READER_ALLOWED_OPS,
  GLOFOX_READER_BADGE_LABEL,
  GLOFOX_READER_COMMAND,
  GLOFOX_READER_PRESET_ID,
  glofoxReaderBadge,
  glofoxReaderBlockers,
  glofoxReaderNeedsProvider,
  glofoxReaderPermissionRows,
} from "../src/lib/glofoxReader.js";

// ── 1. the badge is a pure function of SERVER state ──────────────────
{
  assert.equal(glofoxReaderBadge({ glofox_reader: true }), true);
  assert.equal(glofoxReaderBadge({ glofox_reader: false }), false);
  assert.equal(glofoxReaderBadge({}), false);
  assert.equal(glofoxReaderBadge(null), false);
  assert.equal(glofoxReaderBadge(undefined), false);
  // A non-boolean server value is NOT trusted (exact `true` only).
  assert.equal(glofoxReaderBadge({ glofox_reader: "true" }), false);
  assert.equal(glofoxReaderBadge({ glofox_reader: 1 }), false);
  // The UI never infers the badge from a policy shape or a preset name.
  assert.equal(
    glofoxReaderBadge({ policy: { "glofox:read": 0 } }), false,
    "a policy alone must not produce a badge");
  assert.equal(GLOFOX_READER_BADGE_LABEL, "Glofox Reader");
}

// ── 2. the granted operation is exactly the pinned schedule read ─────
{
  assert.deepEqual(
    [...GLOFOX_READER_ALLOWED_OPS].sort(),
    ["glofox:read"],
    "a Glofox Reader is granted the pinned Level 6 schedule read only");
  // Every other capability is NOT in the allowed set — including the whole
  // browser surface (a Glofox Reader cannot browse at all).
  for (const op of [
    "browser:navigate", "browser:read",
    "browser:click", "browser:screenshot", "browser:type", "browser:upload",
    "browser:download", "browser:submit", "browser:delete",
    "fs:write", "fs:delete", "repo:pr", "repo:push",
    "mail:send", "cal:create", "bot:delegate",
  ]) {
    assert.equal(GLOFOX_READER_ALLOWED_OPS.includes(op), false,
      `${op} must never be allowed`);
  }
}

// ── 3. the ONE owner-facing command is byte-exact ────────────────────
{
  assert.equal(GLOFOX_READER_COMMAND, "glofox: schedule");
  for (const variant of [
    "glofox:  schedule",
    "GLOFOX: schedule",
    "glofox: schedule tomorrow",
    "glofox",
  ]) {
    assert.notEqual(GLOFOX_READER_COMMAND, variant,
      `the command must not absorb ${JSON.stringify(variant)}`);
  }
}

// ── 4. the permission preview shapes the server payload verbatim ─────
{
  const server = {
    "id": "glofox-reader",
    "policy": { "glofox:read": 0 },
    // Exactly what serve.effective_permissions returns server-side.
    "permissions": Object.fromEntries(
      ["browser:navigate", "browser:read", "browser:click",
       "browser:screenshot", "browser:type", "browser:upload",
       "browser:download", "browser:submit", "browser:delete",
       "fs:read", "fs:write", "fs:delete", "repo:read", "repo:pr",
       "repo:push", "mail:send", "mail:read", "cal:read", "cal:create",
       "glofox:read", "bot:delegate"]
        .map((op) => [op, op === "glofox:read" ? 0 : "deny"])),
  };
  const rows = glofoxReaderPermissionRows(server);
  // Sorted by operation; the granted op appears once, at tier 0.
  assert.deepEqual(
    rows.map(([op]) => op),
    [...rows.map(([op]) => op)].sort((a, b) => a.localeCompare(b)));
  assert.deepEqual(
    rows.find(([op]) => op === "glofox:read"), ["glofox:read", 0]);
  for (const [op, tier] of rows) {
    if (op !== "glofox:read") {
      assert.equal(tier, "deny", `${op} must be denied, got ${tier}`);
    }
  }
  // Malformed/missing payloads degrade to no rows, never a crash.
  assert.deepEqual(glofoxReaderPermissionRows(null), []);
  assert.deepEqual(glofoxReaderPermissionRows({}), []);
}

// ── 5. create-surface identification ─────────────────────────────────
{
  // The preset id the create form submits to the EXISTING create/configure
  // endpoint must match the backend preset id byte-for-byte.
  assert.equal(GLOFOX_READER_PRESET_ID, "glofox-reader");
}

// ── 6. the confirmation modal's confirm/cancel eligibility ───────────
{
  // A clean surface (no allowlist, no host binding) is CONFIGURABLE — the
  // modal's Confirm stays enabled (mirrors the server accepting the preset).
  const clean = { browser_allowlist: [] };
  assert.deepEqual(glofoxReaderBlockers(clean, { boundHostId: "" }), [],
    "a clean surface must have no blockers");
  assert.equal(canConfigureGlofoxReader(clean, { boundHostId: "" }), true);

  // A browser allowlist in the roster payload is a blocker (server 409).
  const allowlisted = { browser_allowlist: ["example.com"] };
  assert.deepEqual(
    glofoxReaderBlockers(allowlisted, { boundHostId: "" }).length > 0, true);
  assert.equal(canConfigureGlofoxReader(allowlisted, { boundHostId: "" }), false,
    "an allowlist must block the configure");

  // A Browser Host binding is a blocker (server 409).
  assert.equal(
    canConfigureGlofoxReader(clean, { boundHostId: "host-a" }), false,
    "a Browser Host binding must block the configure");
  assert.deepEqual(
    glofoxReaderBlockers(clean, { boundHostId: "host-a" }),
    ["a clean browser surface (unbind the Browser Host)"]);

  // Whitespace-only and non-array allowlists fail closed to usable shapes:
  // an entry that trims EMPTY is not an allowlist, a broken payload is not
  // trusted as either state (no blockers invented, none hidden behind it).
  for (const bad of [
    { browser_allowlist: ["   "] },
    { browser_allowlist: null },
    {}, null, undefined,
  ]) {
    assert.equal(
      canConfigureGlofoxReader(bad, { boundHostId: "" }), true,
      `${JSON.stringify(bad)} — the SERVER decides allowlist cleanliness`);
  }
  // But a REAL hostname is always seen (never silently swallowed).
  assert.equal(canConfigureGlofoxReader(
    { browser_allowlist: [null, "a.com"] }, { boundHostId: "" }), false);

  // A missing provider profile is a WARN, never a blocker: a Bot may hold
  // the schedule-read grant before an LLM is assigned (it just cannot serve).
  assert.equal(glofoxReaderNeedsProvider({}), true);
  assert.equal(glofoxReaderNeedsProvider(null), true);
  assert.equal(
    glofoxReaderNeedsProvider({ provider_profile_id: "p1" }), false);
  assert.equal(
    glofoxReaderNeedsProvider({ provider_profile_id: "" }), true);
}
