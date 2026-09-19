// calendarWriter.test.mjs — the Calendar Writer surface's deterministic
// decisions (pure; no React, no network).
//
//   1. the WRITE-CAPABILITY badge is derived EXCLUSIVELY from the server's own
//      `calendar_writer` flag — never re-derived from a policy or a local guess,
//      and a non-boolean server value is not trusted;
//   2. the granted operation is exactly the event create (never cal:list, never
//      any browser/write/mail/coordination op);
//   3. the entitlement preview shapes the server payload verbatim;
//   4. a Calendar Writer has NO browser surface (allowlist/host are blockers).
//
// Run: node tests/calendarWriter.test.mjs
import assert from "node:assert/strict";
import {
  CALENDAR_WRITER_ALLOWED_OPS,
  CALENDAR_WRITER_BADGE_LABEL,
  CALENDAR_WRITER_GRAMMAR,
  CALENDAR_WRITER_PRESET_ID,
  CALENDAR_WRITER_POLICY,
  calendarWriterBadge,
  calendarWriterBlockers,
  calendarWriterNeedsProvider,
  calendarWriterPermissionRows,
  canConfigureCalendarWriter,
  isCalendarWriterPolicy,
} from "../src/lib/calendarWriter.js";

// 1. the badge is a pure function of SERVER state.
{
  assert.equal(calendarWriterBadge({ calendar_writer: true }), true);
  assert.equal(calendarWriterBadge({ calendar_writer: false }), false);
  assert.equal(calendarWriterBadge({}), false);
  assert.equal(calendarWriterBadge(null), false);
  assert.equal(calendarWriterBadge(undefined), false);
  // A non-boolean server value is NOT trusted (exact `true` only).
  assert.equal(calendarWriterBadge({ calendar_writer: "true" }), false);
  assert.equal(calendarWriterBadge({ calendar_writer: 1 }), false);
  // The UI never infers the badge from a policy shape.
  assert.equal(calendarWriterBadge({ policy: { "cal:create": 0 } }), false);
  assert.equal(CALENDAR_WRITER_BADGE_LABEL, "Calendar Writer");
}

// 2. the granted operation is exactly the event create.
{
  assert.deepEqual([...CALENDAR_WRITER_ALLOWED_OPS].sort(), ["cal:create"]);
  for (const op of [
    "cal:list", "browser:navigate", "browser:read", "browser:click",
    "fs:write", "fs:delete", "repo:pr", "repo:push", "mail:send",
    "bot:delegate",
  ]) {
    assert.equal(CALENDAR_WRITER_ALLOWED_OPS.includes(op), false,
      `${op} must never be allowed`);
  }
  assert.deepEqual(CALENDAR_WRITER_POLICY, { "cal:create": 0 });
  assert.equal(isCalendarWriterPolicy({ "cal:create": 0 }), true);
  assert.equal(isCalendarWriterPolicy({ "cal:create": 1 }), false);
  assert.equal(isCalendarWriterPolicy({ "cal:list": 0 }), false);
  assert.equal(isCalendarWriterPolicy({ "cal:create": 0, "cal:list": 0 }), false);
  assert.equal(isCalendarWriterPolicy(null), false);
  assert.equal(CALENDAR_WRITER_PRESET_ID, "calendar-writer");
  assert.equal(CALENDAR_WRITER_GRAMMAR,
    "create <title> on YYYY-MM-DD from HH:MM to HH:MM");
}

// 3. the permission preview shapes the server payload verbatim.
{
  const server = {
    id: "calendar-writer",
    policy: { "cal:create": 0 },
    permissions: Object.fromEntries(
      ["browser:navigate", "browser:read", "fs:write", "fs:delete",
       "repo:pr", "repo:push", "mail:send", "cal:list", "cal:create",
       "bot:delegate"].map((op) => [op, op === "cal:create" ? 0 : "deny"])),
  };
  const rows = calendarWriterPermissionRows(server);
  assert.deepEqual(
    rows.map(([op]) => op),
    [...rows.map(([op]) => op)].sort((a, b) => a.localeCompare(b)));
  assert.deepEqual(rows.find(([op]) => op === "cal:create"), ["cal:create", 0]);
  for (const [op, tier] of rows) {
    if (op !== "cal:create") assert.equal(tier, "deny", op);
  }
  assert.deepEqual(calendarWriterPermissionRows(null), []);
  assert.deepEqual(calendarWriterPermissionRows({}), []);
}

// 4. no browser surface — allowlist / host binding block it.
{
  const clean = { browser_allowlist: [] };
  assert.deepEqual(calendarWriterBlockers(clean, { boundHostId: "" }), []);
  assert.equal(canConfigureCalendarWriter(clean, { boundHostId: "" }), true);
  assert.equal(
    canConfigureCalendarWriter({ browser_allowlist: ["example.com"] },
      { boundHostId: "" }), false);
  assert.equal(canConfigureCalendarWriter(clean, { boundHostId: "h1" }), false);
  for (const bad of [{ browser_allowlist: ["   "] }, { browser_allowlist: null },
                     {}, null, undefined]) {
    assert.equal(canConfigureCalendarWriter(bad, { boundHostId: "" }), true);
  }
  assert.equal(calendarWriterNeedsProvider({}), true);
  assert.equal(calendarWriterNeedsProvider({ provider_profile_id: "p1" }), false);
}

console.log("all calendarWriter lib tests passed");
