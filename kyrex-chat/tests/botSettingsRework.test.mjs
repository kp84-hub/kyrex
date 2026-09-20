// botSettingsRework.test.mjs — the reworked user-facing bot model in BotSettings,
// rendered for real in jsdom (React 19) against a mock backend:
//
//   1. Delete lives under the ⋯ three-dot menu with EXPLICIT name confirmation:
//      the Delete button stays disabled until the bot's EXACT current name is
//      typed, and confirm POSTs { confirm_name } to /api/bots/{id}/delete;
//   2. a deleted bot's preserved-data notice names conversations, task history,
//      Google authorization, provider profiles, and calendar events;
//   3. the safe migration card lists the owner's legacy calendar-family bots and
//      consolidates them via POST /api/bots/migrate/legacy-calendar (stopped,
//      never deleted);
//   4. a Calendar Bot's Google panel shows the connected account email + the
//      destination calendar choices, Change calendar PUTs { calendar_id }, and
//      Reconnect Google redirects through the provider authorization URL.
//
// Run: node --import ./tests/jsdomSetup.mjs \
//        --import ./dev/jsx-loader-register.mjs --test tests/botSettingsRework.test.mjs
import assert from "node:assert/strict";
import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import BotSettings from "../src/components/BotSettings.jsx";

const h = React.createElement;

// ── mock backend ─────────────────────────────────────────────────────────
const calls = [];
let legacy = [
  { bot_id: "reader1", name: "Old Reader", status: "running",
    kind: "calendar-reader", kind_label: "can read a calendar" },
  { bot_id: "writer1", name: "Old Writer", status: "paused",
    kind: "calendar-writer", kind_label: "can create calendar events" },
];
let googleAccount = { email: "owner@example.com", calendar_id: "primary" };
const googleCalendars = [
  { id: "primary", summary: "Owner", primary: true, preferred: true },
  { id: "work@example.com", summary: "Work", primary: false, preferred: false },
];
let redirectTarget = null;

function resp(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 409 ? "Conflict" : "OK",
    async json() { return body; },
  };
}

globalThis.fetch = async (url, opts = {}) => {
  const method = (opts.method || "GET").toUpperCase();
  const body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({ url, method, body });

  if (url === "/api/bots/presets") {
    return resp({
      presets: [
        { id: "calendar", label: "Calendar Bot",
          permissions: { "cal:list": 0, "cal:create": 0, "glofox:read": 0 } },
      ],
      capabilities: [
        { id: "chief-of-staff", label: "Chief of Staff", preset: "coordinator",
          description: "Coordinates your other Bots by delegating work to them." },
        { id: "calendar", label: "Calendar Bot", preset: "calendar",
          description: "Reads your Google calendar and creates events with your explicit approval." },
        { id: "developer", label: "Developer Bot", preset: "developer",
          description: "Works on a real repository." },
        { id: "browser", label: "Browser Bot", preset: "browser",
          description: "Reads pages on allowlisted domains." },
      ],
    });
  }
  if (url === "/api/chat/provider-profiles") return resp({ profiles: [] });
  if (url === "/api/chat/workspaces") return resp({ workspaces: [] });

  if (url === "/api/bots/migrate" && method === "GET") {
    return resp({ legacy, calendar_bot_id: "" });
  }
  if (url === "/api/bots/migrate/legacy-calendar" && method === "POST") {
    const stopped = legacy.map((l) => ({ bot_id: l.bot_id, name: l.name,
                                         migrated_to: "calendar-1" }));
    legacy = [];
    return resp({ calendar_bot_id: "calendar-1", moved: [], stopped,
                  errors: [] });
  }
  if (url.endsWith("/delete") && method === "POST") {
    assert.equal(body.confirm_name, "Cal Bot", "exact name required");
    return resp({ deleted: "calbot", name: "Cal Bot",
      preserved: ["conversations", "task_history", "google_authorization",
                  "provider_profiles", "calendar_events"] });
  }
  if (url === "/api/connections/google/account") {
    return resp(googleAccount);
  }
  if (url === "/api/connections/google/calendars") {
    return resp({ calendars: googleCalendars, preferred: googleAccount.calendar_id });
  }
  if (url === "/api/connections/google/calendar" && method === "PUT") {
    googleAccount = { ...googleAccount, calendar_id: body.calendar_id };
    return resp({ calendar_id: body.calendar_id,
                  preferred: body.calendar_id });
  }
  if (url === "/api/connections/google/connect" && method === "POST") {
    return resp({ provider: "google",
      authorization_url: "https://accounts.google.com/o/oauth2/v2/auth?state=abc" });
  }
  return resp({});
};

// ── harness ──────────────────────────────────────────────────────────────
const CAL_BOT = {
  id: "calbot", name: "Cal Bot", status: "running", model: "anthropic:claude",
  available: true, manageable: true, claimable: false,
  coordinator: false, browser_bot: false, calendar_bot: true,
  browser_allowlist: [],
  role: { id: "calendar", label: "Calendar Bot",
          description: "Reads your Google calendar and creates events with approval." },
};

let onChangedCount = 0;
const container = document.createElement("div");
container.id = "root";
document.body.appendChild(container);
const root = createRoot(container);

const buttons = () => [...container.querySelectorAll("button")];
const byText = (t) => buttons().find((b) => b.textContent.trim() === t);
const byTextRe = (re) => buttons().find((b) => re.test(b.textContent.trim()));
const moreBtn = () => container.querySelector(".bot-more-btn");
const get = (sel) => container.querySelector(sel);

function change(el, value) {
  el.value = value;
  el.dispatchEvent(new window.Event("change", { bubbles: true }));
}

// React 19 controlled inputs: set the value through the native setter so the
// onChange handler observes it, then fire a bubbling input event.
function typeInto(el, value) {
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, "value").set;
  setter.call(el, value);
  el.dispatchEvent(new window.Event("input", { bubbles: true }));
}

async function main() {
  // Stub window.location (a plain object) so Reconnect Google's
  // location.assign(...) is observable in jsdom instead of raising
  // "Not implemented: navigation".
  const origWindow = window;
  // A window Proxy that returns a plain fake location, whose assign captures
  // the redirect (jsdom's own location.assign would raise "Not implemented:
  // navigation", and its assign property is non-configurable so it cannot be
  // proxied directly).
  const fakeLocation = {
    assign: (u) => { redirectTarget = String(u); },
    href: origWindow.location.href, protocol: origWindow.location.protocol,
    host: origWindow.location.host, hostname: origWindow.location.hostname,
    port: origWindow.location.port, pathname: origWindow.location.pathname,
    search: origWindow.location.search, hash: origWindow.location.hash,
    origin: origWindow.location.origin, reload() {},
  };
  const proxiedWindow = new Proxy(origWindow, {
    get(target, prop, receiver) {
      if (prop === "location") return fakeLocation;
      return Reflect.get(target, prop, receiver);
    },
  });
  Object.defineProperty(globalThis, "window", {
    value: proxiedWindow, configurable: true,
  });

  await act(async () => {
    root.render(h(BotSettings, {
      bots: [CAL_BOT], onClose() {}, onChanged() { onChangedCount += 1; },
    }));
  });
  await act(async () => {}); // let the presets/migrate fetches settle

  // ── 1. Delete: ⋯ menu → Delete bot → exact-name confirmation ─────────
  assert.ok(moreBtn(), "three-dot menu button rendered");
  await act(async () => { moreBtn().click(); });
  const deleteItem = byText("Delete bot");
  assert.ok(deleteItem, "Delete bot item in the ⋯ menu");
  await act(async () => { deleteItem.click(); });
  const dialog = get('[aria-label="Delete bot"]');
  assert.ok(dialog, "delete dialog opened");
  assert.match(dialog.textContent, /Cal Bot/);
  assert.match(dialog.textContent, /preserved|conversations, task history, Google authorization/);

  const input = get("#delete-confirm-calbot");
  const confirmBtn = byText("Delete bot");
  assert.equal(confirmBtn.disabled, true, "delete disabled before name echo");
  await act(async () => {
    typeInto(input, "wrong name");
  });
  assert.equal(byText("Delete bot").disabled, true, "wrong name keeps delete disabled");
  await act(async () => {
    typeInto(input, "Cal Bot");
  });
  assert.equal(byText("Delete bot").disabled, false, "exact name enables delete");
  const nBefore = calls.length;
  await act(async () => { byText("Delete bot").click(); });
  await act(async () => {});
  const delCall = calls.slice(nBefore).find((c) => c.url.endsWith("/delete"));
  assert.ok(delCall, "delete POST issued");
  assert.equal(delCall.method, "POST");
  assert.deepEqual(delCall.body, { confirm_name: "Cal Bot" });
  assert.match(container.textContent, /was deleted/);
  assert.ok(onChangedCount >= 1, "roster refreshed after delete");
  console.log("ok - delete requires the exact name and POSTs confirm_name; roster refreshed");

  // ── 2. migration card detects legacy bots; consolidate never deletes ──
  await act(async () => {
    root.render(h(BotSettings, {
      bots: [CAL_BOT], onClose() {}, onChanged() {},
    }));
  });
  await act(async () => {});
  const card = get('[data-testid="bot-migration"]');
  assert.ok(card, "migration card rendered");
  assert.match(card.textContent, /Old Reader/);
  assert.match(card.textContent, /can read a calendar/);
  assert.match(card.textContent, /Old Writer/);
  const n2 = calls.length;
  await act(async () => { byTextRe(/Consolidate into one Calendar Bot/).click(); });
  await act(async () => {});
  const migCall = calls.slice(n2).find(
    (c) => c.url === "/api/bots/migrate/legacy-calendar");
  assert.ok(migCall, "consolidate POST issued");
  const result = get('[data-testid="migration-result"]');
  assert.ok(result, "migration result rendered");
  assert.match(result.textContent, /calendar-1/);
  assert.match(result.textContent, /Stopped \(never deleted\): reader1, writer1/);
  console.log("ok - migration consolidates and reports stopped (never deleted)");

  // ── 3. Calendar Bot Google panel: account, change calendar, reconnect ─
  await act(async () => { byText("Google account & calendar").click(); });
  await act(async () => {});
  const panel = get('[data-testid="google-calbot"]');
  assert.ok(panel, "Google panel rendered for a Calendar Bot");
  assert.match(panel.textContent, /owner@example\.com/);
  const destSelect = get("#calendar-dest-calbot");
  assert.ok(destSelect, "destination calendar selector rendered");
  assert.equal(destSelect.value, "primary");
  const n3 = calls.length;
  await act(async () => { change(destSelect, "work@example.com"); });
  await act(async () => { byText("Change calendar").click(); });
  await act(async () => {});
  const putCall = calls.slice(n3).find(
    (c) => c.url === "/api/connections/google/calendar" && c.method === "PUT");
  assert.ok(putCall, "Change calendar PUT issued");
  assert.deepEqual(putCall.body, { calendar_id: "work@example.com" });

  redirectTarget = null;
  const n4 = calls.length;
  await act(async () => { byText("Reconnect Google").click(); });
  await act(async () => {});
  const connCall = calls.slice(n4).find(
    (c) => c.url === "/api/connections/google/connect" && c.method === "POST");
  assert.ok(connCall, "reconnect starts the OAuth round-trip");
  assert.ok(redirectTarget && redirectTarget.startsWith("https://accounts.google.com"),
    "reconnect redirects to the provider authorization URL");
  console.log("ok - Google panel shows account + calendar, change PUTs, reconnect redirects");

  console.log("all botSettingsRework tests passed");
  Object.defineProperty(globalThis, "window", {
    value: origWindow, configurable: true,
  });
}

await main();
await act(async () => { root.unmount(); });
globalThis.window.close();
process.exit(0);
