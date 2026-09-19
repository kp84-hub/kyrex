import test from "node:test";
import assert from "node:assert/strict";
import {
  statusOf, statusLabelOf, primaryActionOf, capabilityLines, safeText,
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
