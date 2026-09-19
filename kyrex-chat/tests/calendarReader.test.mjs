import test from "node:test";
import assert from "node:assert/strict";
import {
  CALENDAR_COMMANDS, CALENDAR_READER_PRESET_ID, CALENDAR_READER_POLICY,
  isCalendarReaderPolicy, isCalendarCommand, isCalendarNamespace,
  commandVerdict, calendarReaderBadge,
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

test("badge only appears for an exact Calendar Reader", () => {
  assert.deepEqual(calendarReaderBadge({ policy: { "cal:list": 0 } }),
    { label: "Calendar Reader", tone: "ok" });
  assert.equal(calendarReaderBadge({ policy: { "fs:write": 1 } }), null);
});
