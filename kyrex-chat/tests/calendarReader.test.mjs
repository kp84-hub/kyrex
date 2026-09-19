// calendarReader.test.mjs — the Calendar Reader surface's deterministic
// decisions.
//
// Proves, WITHOUT rendering React and WITHOUT any network:
//   1. the badge is derived EXCLUSIVELY from the server's own `calendar_reader`
//      flag — never re-derived from the policy or a local guess, and a
//      non-boolean server value is not trusted;
//   2. the effective-permission rows come straight from the server preset and
//      the only granted operation is the pinned `cal:list` read;
//   3. the preset identity is EXACTLY {cal:list:0} and the reserved command
//      namespace is byte-exact;
//   4. the server's 409 blockers (clean browser surface) are mirrored for the
//      confirmation copy;
//   5. the create/configure surface identifies the preset with the smallest
//      support needed: the preset option id and the badge label.
import test from "node:test";
import assert from "node:assert/strict";
import {
  CALENDAR_COMMANDS, CALENDAR_READER_ALLOWED_OPS, CALENDAR_READER_BADGE_LABEL,
  CALENDAR_READER_LABEL, CALENDAR_READER_POLICY, CALENDAR_READER_PRESET_ID,
  calendarReaderBadge, calendarReaderBlockers, calendarReaderNeedsProvider,
  calendarReaderPermissionRows, canConfigureCalendarReader, commandVerdict,
  isCalendarCommand, isCalendarNamespace, isCalendarReaderPolicy,
} from "../src/lib/calendarReader.js";

test("the three commands are exactly today/tomorrow/week", () => {
  assert.deepEqual([...CALENDAR_COMMANDS],
    ["calendar: today", "calendar: tomorrow", "calendar: week"]);
});

test("isCalendarCommand is byte-exact (modulo outer whitespace)", () => {
  assert.equal(isCalendarCommand("calendar: today"), true);
  assert.equal(isCalendarCommand("  calendar: week  "), true);
  assert.equal(isCalendarCommand("calendar: yesterday"), false);
  assert.equal(isCalendarCommand("calendar:today"), false);
  assert.equal(isCalendarCommand("Calendar: today"), false);
});

test("reserved namespace is detected case-insensitively", () => {
  assert.equal(isCalendarNamespace("calendar: whatever"), true);
  assert.equal(isCalendarNamespace("CALENDAR: x"), true);
  assert.equal(isCalendarNamespace("calendarx"), false);
});

test("commandVerdict: command | unsupported | text", () => {
  assert.equal(commandVerdict("calendar: today"), "command");
  assert.equal(commandVerdict("calendar: yesterday"), "unsupported");
  assert.equal(commandVerdict("hello there"), "text");
});

test("preset predicate is exactly {cal:list:0}", () => {
  assert.equal(CALENDAR_READER_PRESET_ID, "calendar-reader");
  assert.equal(isCalendarReaderPolicy(CALENDAR_READER_POLICY), true);
  assert.equal(isCalendarReaderPolicy({ "cal:list": 1 }), false);
  assert.equal(isCalendarReaderPolicy({ "cal:list": 0, "fs:write": 1 }), false);
  assert.equal(isCalendarReaderPolicy({ "*": 0 }), false);
  assert.equal(isCalendarReaderPolicy(null), false);
});

test("the badge is a pure function of SERVER state", () => {
  assert.equal(calendarReaderBadge({ calendar_reader: true }), true);
  assert.equal(calendarReaderBadge({ calendar_reader: false }), false);
  assert.equal(calendarReaderBadge({}), false);
  assert.equal(calendarReaderBadge(null), false);
  assert.equal(calendarReaderBadge(undefined), false);
  // A non-boolean server value is NOT trusted (exact `true` only).
  assert.equal(calendarReaderBadge({ calendar_reader: "true" }), false);
  assert.equal(calendarReaderBadge({ calendar_reader: 1 }), false);
  // The UI never infers the badge from a policy shape or a preset name.
  assert.equal(calendarReaderBadge({ policy: { "cal:list": 0 } }), false);
});

test("permission rows come from the server preset, sorted", () => {
  const preset = { permissions: {
    "fs:write": "deny", "cal:list": 0, "browser:navigate": "deny",
  } };
  assert.deepEqual(calendarReaderPermissionRows(preset),
    [["browser:navigate", "deny"], ["cal:list", 0], ["fs:write", "deny"]]);
  assert.deepEqual(calendarReaderPermissionRows(undefined), []);
  assert.deepEqual(CALENDAR_READER_ALLOWED_OPS, ["cal:list"]);
});

test("blockers mirror the server's clean-browser-surface 409 gates", () => {
  assert.deepEqual(calendarReaderBlockers({}), []);
  assert.equal(canConfigureCalendarReader({}), true);
  assert.equal(
    canConfigureCalendarReader({ browser_allowlist: ["example.com"] }), false);
  assert.equal(calendarReaderBlockers(
    { browser_allowlist: ["example.com"] }).length, 1);
  assert.equal(calendarReaderBlockers(
    {}, { boundHostId: "ovh-ny-01" }).length, 1);
  assert.equal(canConfigureCalendarReader(
    {}, { boundHostId: "ovh-ny-01" }), false);
  // An empty/whitespace allowlist is NOT a blocker.
  assert.equal(canConfigureCalendarReader({ browser_allowlist: [] }), true);
  assert.equal(canConfigureCalendarReader({ browser_allowlist: ["  "] }), true);
});

test("needsProvider warns only when no provider profile is set", () => {
  assert.equal(calendarReaderNeedsProvider({}), true);
  assert.equal(calendarReaderNeedsProvider({ provider_profile_id: "" }), true);
  assert.equal(calendarReaderNeedsProvider({ provider_profile_id: "p1" }), false);
});

test("the create surface identifies the preset by id and badge label", () => {
  assert.equal(CALENDAR_READER_PRESET_ID, "calendar-reader");
  assert.equal(CALENDAR_READER_BADGE_LABEL, "Calendar Reader");
  assert.equal(CALENDAR_READER_LABEL, CALENDAR_READER_BADGE_LABEL);
});
