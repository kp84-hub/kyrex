// connectionsHub.test.mjs — the Muse-style Connections hub, rendered in jsdom.
//
// Proves: search over Connected/Available sections; Google Calendar is the only
// connectable connector; an unimplemented app (Gmail) is a disabled "Coming
// soon" card and never connectable; no token/secret ever reaches the DOM (even
// when the backend view carries hostile capability strings); Connect starts a
// server-side OAuth round-trip; and Calendar event creation stays a SEPARATE,
// explicitly approval-gated control.
//
// Run (from kyrex-chat/):
//   node --import ./tests/jsdomSetup.mjs \
//        --import ./dev/jsx-loader-register.mjs \
//        tests/connectionsHub.test.mjs
import assert from "node:assert/strict";
import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import ConnectionsSettings from "../src/components/ConnectionsSettings.jsx";

const h = React.createElement;

function googleView(overrides = {}) {
  return {
    provider: "google",
    status: "disconnected",
    connected: false,
    expired: false,
    usable: false,
    configured: true,
    read_only: true,
    has_write_scope: false,
    scopes: [],
    access_token: "ya29.FAKE-SECRET-TOKEN-1234567890",
    refresh_token: "1//FAKE-SECRET-REFRESH-1234567890",
    client_secret: "GOCSPX-FAKE-SECRET-0123456789",
    ...overrides,
  };
}

let serverView = googleView();
const calls = [];
globalThis.fetch = async (url, opts = {}) => {
  const method = (opts.method || "GET").toUpperCase();
  calls.push({ url, method });
  const json = (body) => ({ ok: true, status: 200, async json() { return body; } });
  if (method === "GET" && url.endsWith("/connections")) {
    return json({ connectors: [serverView], read_only: true });
  }
  if (url.endsWith("/connections/google/connect")) {
    return json({ provider: "google",
      authorization_url: "https://accounts.google.com/o/oauth2/v2/auth?state=FAKE" });
  }
  if (url.endsWith("/connections/google/upgrade-write")) {
    return json({ provider: "google",
      authorization_url: "https://accounts.google.com/o/oauth2/v2/auth?state=WRITE" });
  }
  if (url.endsWith("/connections/google/upgrade-gmail")) {
    return json({ provider: "google",
      authorization_url: "https://accounts.google.com/o/oauth2/v2/auth?state=GMAIL" });
  }
  if (url.endsWith("/connections/google/disconnect")) {
    serverView = googleView();
    return json({ disconnected: true });
  }
  return json({});
};

const opened = [];
globalThis.window.open = (u) => { opened.push(u); return { closed: false }; };

const makeDiv = () => {
  const d = document.createElement("div");
  document.body.appendChild(d);
  return d;
};
async function renderHub(container) {
  const root = createRoot(container);
  await act(async () => {
    root.render(h(ConnectionsSettings, { onClose() {} }));
  });
  return root;
}
const sectionByTitle = (scope, title) =>
  [...scope.querySelectorAll(".connections-section")].find(
    (s) => s.querySelector(".connections-section-title")?.textContent.trim() === title);
const cardByName = (scope, name) =>
  [...scope.querySelectorAll(".connector-card")].find(
    (c) => c.querySelector(".connector-identity strong")?.textContent.trim() === name);
const buttonByText = (scope, text) =>
  [...scope.querySelectorAll("button")].find((b) => b.textContent.trim() === text);

async function main() {
  // ── disconnected: Calendar + Gmail under Available ─────────────────
  const c1 = makeDiv();
  const root1 = await renderHub(c1);

  assert.ok(c1.querySelector(".connections-search"), "search input rendered");
  assert.equal(sectionByTitle(c1, "Connected"), undefined, "no Connected section yet");
  assert.ok(sectionByTitle(c1, "Available"), "Available section rendered");

  const calendar = cardByName(c1, "Google Calendar");
  const gmail = cardByName(c1, "Gmail");
  assert.ok(calendar && gmail, "both connector cards render");
  assert.ok(buttonByText(calendar, "Connect Google Calendar"), "Calendar offers Connect");
  assert.equal(buttonByText(gmail, "Connect Google Calendar"), undefined,
    "Gmail has NO Connect control (unimplemented)");
  const gmailOnly = gmail.querySelector("button");
  assert.equal(gmailOnly.textContent.trim(), "Coming soon");
  assert.equal(gmailOnly.disabled, true, "Gmail's only control is disabled");
  assert.equal(c1.querySelector(".write-access"), null,
    "no write upgrade before the connector is connected");
  assert.equal(c1.innerHTML.includes("ya29."), false, "no access token rendered");
  assert.equal(c1.innerHTML.includes("1//FAKE"), false, "no refresh token rendered");
  assert.equal(c1.innerHTML.includes("GOCSPX"), false, "no client secret rendered");
  console.log("ok - Available hub; unimplemented Gmail is not connectable");

  // ── search filters ─────────────────────────────────────────────────
  const input = c1.querySelector(".connections-search");
  const typeSearch = (value) => {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "value").set;
    setter.call(input, value);
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
  };
  await act(async () => { typeSearch("gmail"); });
  assert.ok(cardByName(c1, "Gmail"), "Gmail matches the query");
  assert.equal(cardByName(c1, "Google Calendar"), undefined,
    "Google Calendar is filtered out");
  await act(async () => { typeSearch(""); });
  assert.ok(cardByName(c1, "Google Calendar"), "clearing the query restores cards");
  console.log("ok - search filters the hub");

  // ── Connect starts a server-side OAuth round-trip ──────────────────
  opened.length = 0;
  await act(async () => { buttonByText(c1, "Connect Google Calendar").click(); });
  assert.ok(calls.some((c) => c.method === "POST" && c.url.endsWith("/google/connect")),
    "Connect POSTs to the backend");
  assert.equal(opened.length, 1, "the consent URL opens");
  assert.match(opened[0], /^https:\/\/accounts\.google\.com\//);
  await act(async () => { root1.unmount(); });
  console.log("ok - Connect starts a server-side OAuth round-trip");

  // ── connected: Calendar under Connected; write stays separate ──────
  serverView = googleView({ status: "connected", connected: true, usable: true });
  const c2 = makeDiv();
  const root2 = await renderHub(c2);

  const connectedSection = sectionByTitle(c2, "Connected");
  assert.ok(connectedSection, "Connected section rendered");
  assert.ok(cardByName(connectedSection, "Google Calendar"),
    "connected Calendar is under Connected");
  assert.equal(cardByName(connectedSection, "Gmail"), undefined, "Gmail is not connected");
  assert.ok(cardByName(sectionByTitle(c2, "Available"), "Gmail"),
    "Gmail stays under Available");

  const cal2 = cardByName(c2, "Google Calendar");
  assert.ok(cal2.textContent.includes("Read-only"), "read access shown");
  const write = cal2.querySelector(".write-access");
  assert.ok(write, "a separate write-access block is present");
  assert.match(write.textContent, /separate upgrade/);
  assert.match(write.textContent, /explicit approval/);
  assert.ok(buttonByText(write, "Enable calendar event creation"),
    "the write upgrade is an explicit control");

  opened.length = 0;
  await act(async () => { buttonByText(cal2, "Enable calendar event creation").click(); });
  assert.ok(calls.some((c) => c.url.endsWith("/google/upgrade-write")),
    "the write upgrade POSTs to its own route");
  assert.equal(opened.length, 1, "the write consent URL opens");
  await act(async () => { root2.unmount(); });
  console.log("ok - Connected section; Calendar write is separate + approval-gated");

  // ── hostile capability strings redacted; Calendar Reader wording ───
  serverView = googleView({
    status: "connected", connected: true, usable: true,
    capabilities: {
      read_only: true,
      bots: {
        calendar_bot: {
          capabilities: [
            "calendar.read",
            "access_token: ya29.FAKE-SECRET-TOKEN-1234567890",
          ],
          read_only: true,
          unsupported: [
            "calendar.create",
            "client_secret: GOCSPX-FAKE-SECRET-0123456789",
            "refresh_token=1//FAKE-SECRET-REFRESH-1234567890",
          ],
        },
      },
    },
  });
  const c3 = makeDiv();
  const root3 = await renderHub(c3);
  const cal3 = cardByName(c3, "Google Calendar");
  assert.ok(cal3.textContent.includes("Calendar Reader"),
    "the Google Calendar card reads 'Calendar Reader'");
  assert.equal(cal3.textContent.includes("Mail Bot"), false,
    "no misleading Mail Bot wording on the Google Calendar card");
  assert.equal(c3.innerHTML.includes("ya29."), false, "token-shaped capability redacted");
  assert.equal(c3.innerHTML.includes("GOCSPX"), false, "client-secret-shaped capability redacted");
  assert.equal(c3.innerHTML.includes("1//FAKE"), false, "refresh-token-shaped capability redacted");
  assert.equal(c3.innerHTML.includes("[redacted]"), true, "redaction shown in place");
  assert.ok(cal3.textContent.includes("calendar.read"), "ordinary capability survives");
  await act(async () => { root3.unmount(); });
  console.log("ok - hostile capability strings redacted; 'Calendar Reader' wording");

  // ── Gmail becomes connectable ONLY when the backend advertises it ──
  serverView = googleView({
    status: "disconnected", connected: false, usable: false,
    has_gmail_scope: false,
    capabilities: {
      read_only: true,
      bots: {
        gmail_bot: {
          capabilities: ["gmail.read"],
          read_only: true,
          unsupported: ["gmail.send", "gmail.delete", "gmail.archive"],
        },
      },
    },
  });
  const c4 = makeDiv();
  const root4 = await renderHub(c4);
  const gmail4 = cardByName(c4, "Gmail");
  assert.ok(gmail4, "Gmail card renders");
  assert.ok(buttonByText(gmail4, "Connect Gmail"),
    "an advertised backend makes Gmail connectable");
  assert.equal(gmail4.textContent.includes("Mail Reader"), true,
    "the Gmail card reads 'Mail Reader'");
  assert.equal(gmail4.textContent.includes("Calendar Reader"), false,
    "the Gmail card never shows the Calendar capability list");
  assert.equal(gmail4.textContent.includes("READ-ONLY"), true,
    "the read-only notice is shown on the Gmail card");
  opened.length = 0;
  await act(async () => { buttonByText(gmail4, "Connect Gmail").click(); });
  assert.ok(calls.some((c) => c.method === "POST"
    && c.url.endsWith("/google/upgrade-gmail")),
    "Gmail Connect POSTs to its own read-upgrade route");
  assert.equal(opened.length, 1, "the Gmail consent URL opens");
  assert.match(opened[0], /^https:\/\/accounts\.google\.com\//);
  await act(async () => { root4.unmount(); });
  console.log("ok - Gmail is connectable when the backend advertises gmail.read");

  // ── a granted gmail scope: Connected, read-only, no write block ────
  serverView = googleView({
    status: "connected", connected: true, usable: true, has_gmail_scope: true,
    capabilities: {
      read_only: true,
      bots: { gmail_bot: {
        capabilities: ["gmail.read"], read_only: true,
        unsupported: ["gmail.send"] } },
    },
  });
  const c5 = makeDiv();
  const root5 = await renderHub(c5);
  const conn5 = sectionByTitle(c5, "Connected");
  const gmail5 = cardByName(conn5, "Gmail");
  assert.ok(gmail5, "a granted Gmail scope puts Gmail under Connected");
  assert.ok(gmail5.textContent.includes("Gmail read is enabled (read-only)."),
    "the read-only confirmation is shown");
  assert.equal(gmail5.querySelector(".write-access"), null,
    "Gmail has NO write-access block");
  assert.ok(gmail5.textContent.includes("Read-only"), "Gmail stays read-only");
  assert.equal(c5.innerHTML.includes("ya29."), false, "no token on the Gmail card");
  await act(async () => { root5.unmount(); });
  console.log("ok - granted Gmail scope: Connected, read-only, no write upgrade");

  c1.remove(); c2.remove(); c3.remove(); c4.remove(); c5.remove();
  console.log("connectionsHub.test.mjs — all assertions passed");
}

main().then(() => {
  // jsdom keeps timers on the event loop; close it and exit deterministically.
  globalThis.window.close();
  process.exit(0);
}).catch((error) => {
  console.error(error);
  process.exit(1);
});
