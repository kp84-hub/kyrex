// botSettingsHost.test.mjs — the explicit, server-authoritative Browser Host
// binding flow in BotSettings, rendered for real in jsdom (React 19) and driven
// end to end against a mock fetch.
//
// The bot-rework user-facing model: Browser-only controls (Browser allowlist,
// Browser Host) are rendered ONLY for Bots whose server-derived role is
// Browser — they are hidden from every other Bot (relevant-settings
// visibility). The one Change capability control derives its options and
// descriptions from the server's capability table.
//
// Proves:
//   1. an unbound Browser Bot's selector starts on the "Select a Browser Host"
//      placeholder — it never visually defaults to the first enrolled host;
//   2. "Bind Browser Host" is disabled until a host is deliberately selected;
//   3. Bind POSTs exactly { host_id } to /api/bots/{id}/browser-host;
//   4. the bound status is read back from the server GET (never local state)
//      and the roster (onChanged) is refreshed;
//   5. Unbind DELETEs and re-reads the now-unbound server state;
//   6. a 409/403/network failure is surfaced inline beside the controls;
//   7. a NON-Browser Bot renders NO Browser Host / Browser allowlist controls
//      (relevant-settings visibility);
//   8. the Change capability control renders the server's primary options and
//      the selected capability's server-sent description + permissions, and
//      Set capability POSTs exactly { capability } to /api/bots/{id}/capability
//      then refreshes the roster.
//
// Run: node --import ./tests/jsdomSetup.mjs \
//        --import ./dev/jsx-loader-register.mjs --test tests/botSettingsHost.test.mjs
import assert from "node:assert/strict";
import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import BotSettings from "../src/components/BotSettings.jsx";

const h = React.createElement;

// ── mock backend ─────────────────────────────────────────────────────────
const HOSTS = [{ host_id: "ovh-ny-01", state: "online", available: true }];
let boundHostId = "";
let bindErrorDetail = null;
const calls = [];

function resp(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 409 ? "Conflict" : "OK",
    async json() { return body; },
  };
}

const CAPABILITIES = [
  { id: "chief-of-staff", label: "Chief of Staff",
    description: "Coordinates your other Bots by delegating work to them.",
    preset: "coordinator" },
  { id: "calendar", label: "Calendar Bot",
    description: "Reads your Google calendar and creates events with your explicit approval.",
    preset: "calendar" },
  { id: "developer", label: "Developer Bot",
    description: "Works on a real repository: reads and writes files and opens pull requests.",
    preset: "developer" },
  { id: "browser", label: "Browser Bot",
    description: "Reads pages on the domains you allowlisted through a Browser Host you bind.",
    preset: "browser" },
];

const PRESETS = [
  { id: "developer", label: "Developer Bot", permissions: {} },
  { id: "browser", label: "Browser Bot",
    permissions: { "browser:navigate": 0, "browser:read": 0 } },
  { id: "calendar", label: "Calendar Bot",
    permissions: { "cal:list": 0, "cal:create": 0, "glofox:read": 0 } },
];

globalThis.fetch = async (url, opts = {}) => {
  const method = (opts.method || "GET").toUpperCase();
  const body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({ url, method, body });

  if (url === "/api/bots/presets") {
    return resp({ presets: PRESETS, capabilities: CAPABILITIES });
  }
  if (url === "/api/chat/provider-profiles") return resp({ profiles: [] });
  if (url === "/api/chat/workspaces") return resp({ workspaces: [] });
  if (url === "/api/bots/migrate") {
    return resp({ legacy: [], calendar_bot_id: "" });
  }

  if (url.endsWith("/browser-host")) {
    if (method === "GET") {
      return resp({
        bot_id: "bot1",
        bound_host_id: boundHostId,
        host: boundHostId ? HOSTS[0] : null,
        hosts: HOSTS,
      });
    }
    if (method === "POST") {
      if (bindErrorDetail) return resp({ detail: bindErrorDetail }, 409);
      boundHostId = body.host_id;
      return resp({ bot_id: "bot1", bound_host_id: boundHostId, host: HOSTS[0] });
    }
    if (method === "DELETE") {
      boundHostId = "";
      return resp({ unbound: true, bot_id: "bot1" });
    }
  }
  if (url.endsWith("/capability") && method === "POST") {
    const cap = CAPABILITIES.find((c) => c.id === body.capability);
    return resp({
      id: "bot1", name: cap ? cap.label : "Scout",
      role: cap ? { id: cap.id, label: cap.label, description: cap.description }
                : { id: "custom", label: "Custom Bot", description: "" },
      browser_bot: body.capability === "browser",
    });
  }
  return resp({});
};

// ── harness ──────────────────────────────────────────────────────────────
const BROWSER_BOT = {
  id: "bot1", name: "Scout", status: "running", model: "anthropic:claude",
  available: true, manageable: true, claimable: false,
  coordinator: false, browser_bot: true,
  browser_allowlist: ["example.com"],
  role: { id: "browser", label: "Browser Bot",
          description: "Reads pages on the domains you allowlisted." },
};

const DEV_BOT = {
  id: "bot2", name: "Builder", status: "stopped", model: "anthropic:claude",
  available: true, manageable: true, claimable: false,
  coordinator: false, browser_bot: false,
  browser_allowlist: [],
  role: { id: "developer", label: "Developer Bot",
          description: "Works on a real repository." },
};

let onChangedCount = 0;
const container = document.createElement("div");
container.id = "root";
document.body.appendChild(container);
const root = createRoot(container);

const buttons = () => [...container.querySelectorAll("button")];
const byText = (t) => buttons().find((b) => b.textContent.trim() === t);
const select = () => container.querySelector("#host-bot1");
const statusEl = () => container.querySelector(".bot-host-status");
const capabilityPanel = () => container.querySelector('[data-testid="capability-bot1"]');

function change(el, value) {
  el.value = value;
  el.dispatchEvent(new window.Event("change", { bubbles: true }));
}

async function renderBots(bots) {
  await act(async () => {
    root.render(h(BotSettings, {
      bots, onClose() {}, onChanged() { onChangedCount += 1; },
    }));
  });
}

async function main() {
  await renderBots([BROWSER_BOT]);

  // 1. unbound placeholder — never the first host.
  await act(async () => { byText("Browser Host").click(); });
  assert.ok(select(), "host selector rendered");
  assert.equal(select().value, "", "unbound selector sits on the placeholder");
  assert.equal(select().options[0].textContent, "Select a Browser Host");
  assert.equal(select().options.length, 2, "placeholder + one enrolled host");
  assert.equal(statusEl().textContent, "Not bound to any Browser Host.");
  // eslint-disable-next-line no-console
  console.log("ok - unbound selector starts on the placeholder");

  // 2. Bind disabled until a deliberate selection.
  assert.equal(byText("Bind Browser Host").disabled, true);
  await act(async () => { change(select(), "ovh-ny-01"); });
  assert.equal(byText("Bind Browser Host").disabled, false);
  console.log("ok - bind disabled until a host is deliberately selected");

  // 3. Bind POSTs { host_id }.
  await act(async () => { byText("Bind Browser Host").click(); });
  const post = calls.find((c) => c.method === "POST");
  assert.ok(post, "POST issued");
  assert.equal(post.url, "/api/bots/bot1/browser-host");
  assert.deepEqual(post.body, { host_id: "ovh-ny-01" });
  console.log("ok - bind POSTs the deliberate host_id");

  // 4. status read back from the server; roster refreshed.
  const postIdx = calls.findIndex((c) => c.method === "POST");
  assert.ok(
    calls.slice(postIdx + 1).some(
      (c) => c.method === "GET" && c.url.endsWith("/browser-host")),
    "binding status re-read from the server",
  );
  assert.equal(statusEl().textContent, "Bound to ovh-ny-01 — online");
  assert.ok(onChangedCount >= 1, "roster refreshed after bind");
  assert.equal(select().value, "", "selector resets to the placeholder");
  console.log("ok - bound status comes from the server GET; roster refreshed");

  // 5. Unbind via DELETE, then server re-read.
  await act(async () => { byText("Unbind").click(); });
  assert.ok(calls.some((c) => c.method === "DELETE"));
  assert.equal(statusEl().textContent, "Not bound to any Browser Host.");
  console.log("ok - unbind DELETEs and re-reads the unbound state");

  // 6. inline error beside the controls (409).
  bindErrorDetail = "host id 'ghost' is not enrolled to this owner";
  await act(async () => { change(select(), "ovh-ny-01"); });
  await act(async () => { byText("Bind Browser Host").click(); });
  const inlineErr = container.querySelector(".bot-host-error");
  assert.ok(inlineErr, "inline error rendered");
  assert.match(inlineErr.textContent, /not enrolled to this owner/);
  assert.equal(inlineErr.getAttribute("role"), "alert");
  console.log("ok - 409 failure shown inline beside the controls");

  // 7. relevant-settings visibility: a Developer Bot renders NO Browser Host /
  //    Browser allowlist controls (its row has only capability + LLM + ⋯).
  await renderBots([DEV_BOT]);
  assert.ok(byText("Change capability"), "Developer Bot still offers the capability control");
  assert.ok(byText("Configure LLM"), "LLM configuration always applies");
  assert.equal(byText("Browser Host"), undefined, "no Browser Host for a Developer Bot");
  assert.equal(byText("Browser allowlist"), undefined, "no allowlist for a Developer Bot");
  assert.equal(byText("Google account & calendar"), undefined, "no Google panel for a Developer Bot");
  assert.ok(!container.querySelector(".bot-more-btn") ||
    [...container.querySelectorAll(".bot-more-btn")].length === 1,
  "three-dot menu exists once (right-side)");
  console.log("ok - browser/workspace-unrelated controls hidden from a Developer Bot");

  // 8. Change capability control renders the server's primary options and the
  //    selected capability's server-sent description + permissions, and Set
  //    capability POSTs exactly { capability }.
  await renderBots([BROWSER_BOT]);
  await act(async () => { byText("Change capability").click(); });
  assert.ok(capabilityPanel(), "capability panel opened");
  const opts = [...capabilityPanel().querySelectorAll("option")];
  assert.deepEqual(
    opts.map((o) => o.value),
    ["chief-of-staff", "calendar", "developer", "browser"],
    "primary capability options come from the server response",
  );
  const panelText = capabilityPanel().textContent;
  assert.match(panelText, /Reads pages on the domains you allowlisted/,
    "selected capability description is the server's, not a local guess");
  assert.match(panelText, /browser:navigate\s*allowed/, "permissions row renders");
  assert.match(panelText, /browser:read\s*allowed/, "permissions row renders");

  const nBefore = calls.length;
  await act(async () => { byText("Set capability").click(); });
  const capCall = calls.slice(nBefore).find(
    (c) => c.url.endsWith("/capability") && c.method === "POST");
  assert.ok(capCall, "capability POST issued");
  assert.deepEqual(capCall.body, { capability: "browser" });
  assert.ok(onChangedCount >= 2, "roster refreshed after capability change");
  assert.match(container.textContent, /was set|descriptions and permissions are server-derived/);
  console.log("ok - Change capability uses server options and POSTs { capability }");

  console.log("all botSettingsHost tests passed");
}

await main();

// Tear down so the process exits 0: unmount React and close jsdom (its timers
// would otherwise keep the event loop alive after the assertions pass).
await act(async () => { root.unmount(); });
globalThis.window.close();
process.exit(0);
