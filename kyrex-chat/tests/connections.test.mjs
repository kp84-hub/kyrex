import test from "node:test";
import assert from "node:assert/strict";
import {
  statusOf, statusLabelOf, primaryActionOf, capabilityLines,
  needsWriteUpgrade, safeText, calendarReaderSummary, joinCapabilities,
} from "../src/lib/connections.js";

test("status derivation prefers the backend's derived expired flag", () => {
  assert.equal(statusOf({ status: "connected", expired: true }), "expired");
  assert.equal(statusOf({ status: "connected", expired: false }), "connected");
  assert.equal(statusOf({ status: "disconnected" }), "disconnected");
  assert.equal(statusOf(null), "unknown");
  assert.equal(statusOf({ status: "garbage" }), "unknown");
});

test("each state has a label and a primary action", () => {
  assert.equal(primaryActionOf("connected"), "disconnect");
  assert.equal(primaryActionOf("disconnected"), "connect");
  assert.equal(primaryActionOf("expired"), "reconnect");
  assert.equal(primaryActionOf("unknown"), "refresh");
  assert.match(statusLabelOf("expired"), /reconnect/i);
});

test("write upgrade is offered only for connected read-only access", () => {
  assert.equal(needsWriteUpgrade({
    status: "connected", expired: false, has_write_scope: false,
  }), true);
  assert.equal(needsWriteUpgrade({
    status: "connected", expired: false, has_write_scope: true,
  }), false);
  assert.equal(needsWriteUpgrade({
    status: "disconnected", has_write_scope: false,
  }), false);
  assert.equal(needsWriteUpgrade({
    status: "connected", expired: true, has_write_scope: false,
  }), false);
});

test("capability lines are calendar-only and read-only", () => {
  const lines = capabilityLines({
    capabilities: { bots: { calendar_bot: {
      capabilities: ["calendar.read"], unsupported: ["calendar.create"] } } },
  });
  assert.deepEqual(lines.capabilities, ["calendar.read"]);
  assert.deepEqual(lines.unsupported, ["calendar.create"]);
});

test("safeText redacts secret-shaped material", () => {
  assert.ok(!safeText("access_token: ya29.ABCDEFGHIJKLMNOPQRSTUV").includes("ya29."));
  assert.ok(!safeText('{"refresh_token":"1//ABCDEFGHIJKLMNOP"}').includes("1//ABCDEFGHIJKLMNOP"));
  assert.ok(!safeText("client_secret=GOCSPX-SECRETVALUE").includes("GOCSPX-SECRETVALUE"));
  assert.ok(!safeText("?code=4/0AbCdEfGhIjKl").includes("4/0AbCdEfGhIjKl"));
  assert.equal(safeText("just words"), "just words");
});

test("Calendar Reader summary replaces the Mail Bot row", () => {
  const rows = calendarReaderSummary({
    capabilities: { bots: {
      mail_bot: { capabilities: ["mail.read"], unsupported: ["mail.send"] },
      calendar_bot: {
        capabilities: ["calendar.read", "calendar.events"],
        unsupported: ["calendar.create"],
      },
    } },
  });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].bot, "Calendar Reader");
  assert.deepEqual(rows[0].capabilities, ["calendar.read", "calendar.events"]);
  assert.deepEqual(rows[0].unsupported, ["calendar.create"]);
  assert.ok(!rows.some((r) => /mail/i.test(r.bot)),
    "the mail capability is not surfaced on the Google Calendar card");
  for (const bad of [null, {}, { capabilities: null },
    { capabilities: { bots: null } }]) {
    const [r] = calendarReaderSummary(bad);
    assert.equal(r.bot, "Calendar Reader");
    assert.deepEqual(r.capabilities, []);
    assert.deepEqual(r.unsupported, []);
  }
  assert.equal(joinCapabilities(["calendar.read", "calendar.events"]),
    "calendar.read, calendar.events");
  assert.equal(joinCapabilities([]), "");
});
