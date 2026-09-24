// connectorRegistry.test.mjs — the Connections hub's registry + card model.
//
// Run: node tests/connectorRegistry.test.mjs
import assert from "node:assert/strict";
import {
  ACCESS_READ,
  ACCESS_READ_WRITE,
  COMING_SOON_LABEL,
  CONNECTOR_REGISTRY,
  READ_ONLY_BADGE,
  READ_WRITE_BADGE,
  SECTION_AVAILABLE,
  SECTION_CONNECTED,
  SECRET_FREE_NOTICE,
  WRITE_UPGRADE_NOTICE,
  backendSupports,
  buildHubModel,
  connectorById,
  connectorMatches,
  isConnectable,
  liveViewFor,
  listConnectors,
  safeView,
  searchConnectors,
} from "../src/lib/connectorRegistry.js";

// ── 1. registry shape ───────────────────────────────────────────────
{
  const cal = connectorById("google_calendar");
  assert.ok(cal, "google_calendar is registered");
  assert.equal(cal.name, "Google Calendar");
  assert.equal(cal.provider, "google");
  assert.equal(cal.implemented, true);
  assert.equal(cal.access, ACCESS_READ, "baseline access is read-only");
  assert.ok(cal.writeUpgrade, "a separate write upgrade is declared");
  assert.equal(cal.writeUpgrade.access, ACCESS_READ_WRITE);
  assert.equal(cal.writeUpgrade.approvalGated, true);

  const gmail = connectorById("gmail");
  assert.ok(gmail, "gmail is registered (the read-only OAuth connector)");
  assert.equal(gmail.implemented, true, "the Gmail read backend path exists now");
  assert.equal(gmail.connectable, true,
    "declared connectable — the BACKEND gate decides the live card");
  assert.equal(gmail.access, ACCESS_READ);
  assert.equal(gmail.writeUpgrade, null, "read-only connectors have no write");
  assert.deepEqual(gmail.requiresCapability,
    { bot: "gmail_bot", capability: "gmail.read" },
    "Gmail is connectable ONLY where the backend advertises gmail.read");
  assert.equal(gmail.scopeField, "has_gmail_scope");
  console.log("ok - registry shape (Calendar real; Gmail read-only, backend-gated)");
}

// ── 2. connectability ───────────────────────────────────────────────
{
  assert.equal(isConnectable(connectorById("google_calendar")), true);
  assert.equal(isConnectable(connectorById("gmail")), true,
    "structurally connectable — the backend capability gate is separate");
  assert.equal(isConnectable({ implemented: true, connectable: false }), false);
  assert.equal(isConnectable({ implemented: false, connectable: true }), false);
  assert.equal(isConnectable(null), false);
  console.log("ok - only implemented + declared connectors are connectable");
}

// ── 2b. backendSupport gate ─────────────────────────────────────────
{
  // A connector with no declared requirement is always backend-supported.
  assert.equal(backendSupports(connectorById("google_calendar"), null), true);
  // Gmail needs the backend to advertise gmail.read.
  assert.equal(backendSupports(connectorById("gmail"), null), false);
  assert.equal(backendSupports(connectorById("gmail"), {}), false);
  assert.equal(backendSupports(connectorById("gmail"), {
    capabilities: { bots: { calendar_bot: { capabilities: ["calendar.read"] } } },
  }), false, "another bot's capability does not satisfy gmail.read");
  assert.equal(backendSupports(connectorById("gmail"), {
    capabilities: { bots: { gmail_bot: { capabilities: ["gmail.read"] } } },
  }), true);
  console.log("ok - backendSupports gates Gmail on the advertised capability");
}

// ── 3. search ───────────────────────────────────────────────────────
{
  assert.equal(searchConnectors("").length, CONNECTOR_REGISTRY.length);
  assert.equal(searchConnectors("   ").length, CONNECTOR_REGISTRY.length);
  assert.deepEqual(searchConnectors("gmail").map((c) => c.id), ["gmail"]);
  assert.deepEqual(searchConnectors("calendar").map((c) => c.id), ["google_calendar"]);
  assert.deepEqual(searchConnectors("MAIL").map((c) => c.id), ["gmail"]);
  assert.deepEqual(searchConnectors("zzzz-none"), []);
  assert.ok(connectorMatches(connectorById("gmail"), "read and search"));
  assert.equal(connectorMatches(connectorById("gmail"), "calendar"), false);
  console.log("ok - search matches name/category/description");
}

// ── 4. sections ─────────────────────────────────────────────────────
{
  const disconnected = [{
    provider: "google", status: "disconnected", connected: false,
    expired: false, usable: false, configured: true, read_only: true,
    has_write_scope: false,
  }];
  let hub = buildHubModel(disconnected);
  assert.equal(hub.connected.length, 0, "nothing connected yet");
  assert.deepEqual(hub.available.map((c) => c.id).sort(),
    ["gmail", "google_calendar"]);
  assert.equal(SECTION_CONNECTED, "Connected");
  assert.equal(SECTION_AVAILABLE, "Available");

  const connected = [{
    ...disconnected[0], status: "connected", connected: true, usable: true,
  }];
  hub = buildHubModel(connected);
  assert.deepEqual(hub.connected.map((c) => c.id), ["google_calendar"]);
  assert.deepEqual(hub.available.map((c) => c.id), ["gmail"]);
  assert.ok(!hub.connected.some((c) => c.status === "planned"),
    "a planned app is never in Connected");

  const expired = [{ ...connected[0], expired: true, usable: false }];
  hub = buildHubModel(expired);
  assert.deepEqual(hub.connected.map((c) => c.id), ["google_calendar"]);
  assert.equal(hub.connected[0].expired, true);
  console.log("ok - Connected/Available sections (planned app always Available)");
}

// ── 5. secret safety ────────────────────────────────────────────────
{
  const hostile = {
    provider: "google", status: "connected", connected: true, expired: false,
    usable: true, configured: true, read_only: true, has_write_scope: false,
    connected_at: 1700000000,
    access_token: "ya29.FAKE-SECRET-TOKEN-1234567890",
    refresh_token: "1//FAKE-SECRET-REFRESH-1234567890",
    client_secret: "GOCSPX-FAKE-SECRET-0123456789",
    authorization: "Bearer ya29.FAKE",
    code: "4/0AxyzFAKE-CODE",
    state: "abc123FAKE-STATE",
    api_key: "sk-FAKE-SECRET",
    password: "hunter2",
  };
  const safe = safeView(hostile);
  for (const k of ["access_token", "refresh_token", "client_secret",
    "authorization", "code", "state", "api_key", "password"]) {
    assert.equal(k in safe, false, `${k} must be dropped`);
  }
  assert.equal(safe.connected, true, "safe fields survive");
  assert.equal(safe.connected_at, 1700000000);

  const model = buildHubModel([hostile]);
  const all = [...model.connected, ...model.available];
  const blob = JSON.stringify(all);
  for (const secret of ["ya29.", "1//FAKE", "GOCSPX", "sk-FAKE", "hunter2",
    "4/0Axyz", "abc123FAKE"]) {
    assert.equal(blob.includes(secret), false, `card model leaked ${secret}`);
  }
  for (const card of all) {
    for (const key of Object.keys(card)) {
      assert.equal(
        /(token|secret|authorization|credential|password|api_key)/i.test(key),
        false, `card key ${key} looks secret-shaped`,
      );
    }
  }
  console.log("ok - hostile secret-shaped fields never reach a card");
}

// ── 6. write access is separate + approval-gated ────────────────────
{
  const connected = [{
    provider: "google", status: "connected", connected: true, expired: false,
    usable: true, configured: true, read_only: true, has_write_scope: false,
  }];
  const model = buildHubModel(connected);
  const [card] = model.connected;
  assert.equal(card.access, ACCESS_READ, "connecting grants READ, not write");
  assert.equal(card.hasWriteScope, false);
  assert.ok(card.writeUpgrade, "the write upgrade is a SEPARATE affordance");
  assert.equal(card.writeUpgrade.approvalGated, true);
  assert.notEqual(card.writeUpgrade.access, card.access);

  const [gmailCard] = model.available;
  assert.equal(gmailCard.id, "gmail");
  assert.equal(gmailCard.connectable, false);
  assert.equal(gmailCard.status, "planned");
  assert.equal(gmailCard.writeUpgrade, null);
  assert.equal(COMING_SOON_LABEL, "Coming soon");
  assert.equal(READ_ONLY_BADGE, "Read-only");
  assert.equal(READ_WRITE_BADGE, "Read & write");
  assert.match(WRITE_UPGRADE_NOTICE, /separate upgrade/);
  assert.match(WRITE_UPGRADE_NOTICE, /explicit approval/);
  assert.match(SECRET_FREE_NOTICE, /never displays or stores a token/);
  console.log("ok - read/write stay separate; unimplemented stays planned");
}

// ── 6b. Gmail live ONLY when the backend advertises gmail.read ──────
{
  const googleBase = {
    provider: "google", status: "connected", connected: true, expired: false,
    usable: true, configured: true, read_only: true, has_write_scope: false,
    has_gmail_scope: false,
  };

  // Backend advertises NOTHING => the Gmail path is ABSENT => planned card.
  let model = buildHubModel([{ ...googleBase }]);
  let gmailCard = model.available.find((c) => c.id === "gmail");
  assert.ok(gmailCard, "Gmail stays Available when the backend is absent");
  assert.equal(gmailCard.connectable, false, "absent backend => not connectable");
  assert.equal(gmailCard.status, "planned");

  // Backend advertises gmail.read => the Gmail card becomes live + connectable,
  // but is NOT yet "connected" (Calendar's grant must not leak onto it).
  const advertised = [{
    ...googleBase,
    capabilities: {
      read_only: true,
      bots: { gmail_bot: {
        capabilities: ["gmail.read"], read_only: true,
        unsupported: ["gmail.send", "gmail.delete", "gmail.archive"],
      } },
    },
  }];
  model = buildHubModel(advertised);
  gmailCard = model.available.find((c) => c.id === "gmail");
  assert.ok(gmailCard, "Gmail is Available until its OWN scope is granted");
  assert.equal(gmailCard.connectable, true, "advertised backend => connectable");
  assert.equal(gmailCard.status, "disconnected");
  assert.equal(gmailCard.connected, false,
    "a connected google view does NOT mean Gmail is connected");
  assert.equal(gmailCard.hasWriteScope, false, "Gmail is strictly read-only");

  // Granting ONLY the gmail scope connects the Gmail card, and it STILL has no
  // write scope (Calendar's write badge can never leak onto Gmail).
  model = buildHubModel([{ ...advertised[0], has_gmail_scope: true }]);
  gmailCard = model.connected.find((c) => c.id === "gmail");
  assert.ok(gmailCard, "granted gmail scope => Gmail under Connected");
  assert.equal(gmailCard.status, "connected");
  assert.equal(gmailCard.hasWriteScope, false);
  const calCard = model.connected.find((c) => c.id === "google_calendar");
  assert.ok(calCard && calCard.connected, "Calendar stays connected independently");
  console.log("ok - Gmail card is live ONLY when the backend advertises gmail.read");
}

// ── 7. defensive: malformed inputs never throw ──────────────────────
{
  assert.deepEqual(safeView(null), {});
  assert.deepEqual(safeView("nope"), {});
  const hub = buildHubModel(null, "calendar");
  assert.equal(hub.connected.length, 0);
  assert.deepEqual(hub.available.map((c) => c.id), ["google_calendar"]);
  assert.equal(liveViewFor(connectorById("gmail"), null), null);
  assert.ok(Array.isArray(listConnectors()));
  console.log("ok - malformed inputs degrade safely");
}

console.log("connectorRegistry.test.mjs — all assertions passed");
