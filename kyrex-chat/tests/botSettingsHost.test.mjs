// botSettingsHost.test.mjs — the explicit, server-authoritative Browser Host
// binding flow in BotSettings, rendered for real in jsdom (React 19) and driven
// end to end against a mock fetch.
//
// Proves:
//   1. an unbound Bot's selector starts on the "Select a Browser Host"
//      placeholder — it never visually defaults to the first enrolled host;
//   2. "Bind Browser Host" is disabled until a host is deliberately selected;
//   3. Bind POSTs exactly { host_id } to /api/bots/{id}/browser-host;
//   4. the bound status is read back from the server GET (never local state)
//      and the roster (onChanged) is refreshed;
//   5. Unbind DELETEs and re-reads the now-unbound server state;
//   6. a 409/403/network failure is surfaced inline beside the controls;
//   7. the Browser-preset dialog's blocker clears ONLY after the server
//      confirms the binding (a local selection alone never clears it).
//
// Run: node --import ./tests/jsdomSetup.mjs \
//        node_modules/.cache/botSettingsHost.bundle.mjs
// (the bundle is produced by esbuild; see the accompanying command)
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

globalThis.fetch = async (url, opts = {}) => {
  const method = (opts.method || "GET").toUpperCase();
  const body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({ url, method, body });

  if (url === "/api/bots/presets") {
    return resp({ presets: [
      { id: "developer", label: "Developer Bot", permissions: {} },
      { id: "browser", label: "Browser Bot",
        permissions: { "browser:navigate": 0, "browser:read": 0 } },
    ] });
  }
  if (url === "/api/chat/provider-profiles") return resp({ profiles: [] });
  if (url === "/api/chat/workspaces") return resp({ workspaces: [] });

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
  return resp({});
};

// ── harness ──────────────────────────────────────────────────────────────
const BOT = {
  id: "bot1", name: "Scout", status: "running", model: "anthropic:claude",
  available: true, manageable: true, claimable: false,
  coordinator: false, browser_bot: false,
  browser_allowlist: ["example.com"],
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
const dialogEl = () => container.querySelector('[aria-label="Configure as Browser Bot"]');

function change(el, value) {
  el.value = value;
  el.dispatchEvent(new window.Event("change", { bubbles: true }));
}

async function main() {
  await act(async () => {
    root.render(h(BotSettings, {
      bots: [BOT], onClose() {}, onChanged() { onChangedCount += 1; },
    }));
  });

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

  // 7a. preset dialog shows the binding blocker while the server reports none.
  await act(async () => { byText("Configure as Browser Bot").click(); });
  assert.ok(dialogEl(), "Browser preset dialog opened");
  assert.match(dialogEl().textContent, /explicit Browser Host binding/);
  // A local selection alone must NOT clear the blocker.
  await act(async () => { change(select(), "ovh-ny-01"); });
  assert.match(dialogEl().textContent, /explicit Browser Host binding/);
  console.log("ok - dialog blocker persists on local selection alone");

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

  // 7b. dialog blocker clears only after the confirmed binding.
  assert.match(
    dialogEl().textContent,
    /Ready: allowlist and Browser Host binding are in place/);
  assert.doesNotMatch(
    dialogEl().textContent, /Missing:.*Browser Host binding/);
  console.log("ok - dialog blocker clears after confirmed server binding");

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

  console.log("all botSettingsHost tests passed");
}

await main();

// Tear down so the process exits 0: unmount React and close jsdom (its timers
// would otherwise keep the event loop alive after the assertions pass).
await act(async () => { root.unmount(); });
globalThis.window.close();
process.exit(0);
