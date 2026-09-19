// calendarWriterSettings.test.mjs — the Calendar Writer's VISIBLE surface in
// BotSettings, rendered for real in jsdom (React 19) against a mock backend.
//
// Proves the create/configure option and the badge exist and are wired to the
// server preset — WITHOUT a Calendar Writer in the presets payload the option
// cannot be configured, and the badge only appears for a Bot the SERVER flags:
//   1. the create "Capability" selector offers the calendar-writer option;
//   2. the "Calendar Writer" WRITE-CAPABILITY badge renders for a server-flagged
//      Bot and NOT for an ordinary one;
//   3. the configure button is labelled "Configure"/"Reconfigure" from the
//      server flag and is enabled when the preset is present;
//   4. the confirmation dialog opens and shows the exact request grammar and the
//      event-write authorization / confirmation contract.
//
// Run: node --import ./tests/jsdomSetup.mjs \
//        --import ./dev/jsx-loader-register.mjs ./tests/calendarWriterSettings.test.mjs
import assert from "node:assert/strict";
import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import BotSettings from "../src/components/BotSettings.jsx";

const h = React.createElement;

function resp(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "OK",
    async json() { return body; },
  };
}

globalThis.fetch = async (url, opts = {}) => {
  const method = (opts.method || "GET").toUpperCase();
  if (url === "/api/bots/presets") {
    return resp({ presets: [
      { id: "developer", label: "Developer Bot", permissions: {} },
      { id: "calendar-reader", label: "Calendar Reader",
        policy: { "cal:list": 0 }, permissions: { "cal:list": 0 } },
      { id: "calendar-writer", label: "Calendar Writer",
        policy: { "cal:create": 0 }, write_capability: true,
        permissions: { "cal:create": 0, "cal:list": "deny",
                       "fs:write": "deny", "browser:navigate": "deny" } },
    ] });
  }
  if (url === "/api/chat/provider-profiles") return resp({ profiles: [] });
  if (url === "/api/chat/workspaces") return resp({ workspaces: [] });
  if (url.endsWith("/browser-host") && method === "GET") {
    return resp({ bot_id: "cw1", bound_host_id: "", host: null, hosts: [] });
  }
  return resp({});
};

const BASE = {
  status: "running", model: "openai:gpt-test", available: true,
  manageable: true, claimable: false, coordinator: false, browser_bot: false,
  glofox_reader: false, level6_weekly: false, calendar_reader: false,
  browser_allowlist: [], provider_profile_id: "p1",
};
const WRITER = { ...BASE, id: "cw1", name: "Writer", calendar_writer: true };
const PLAIN = { ...BASE, id: "plain", name: "Plain", calendar_writer: false };

const container = document.createElement("div");
document.body.appendChild(container);
const root = createRoot(container);

const buttons = () => [...container.querySelectorAll("button")];
const byText = (t) => buttons().find((b) => b.textContent.trim() === t);

async function main() {
  await act(async () => {
    root.render(h(BotSettings, {
      bots: [WRITER, PLAIN], onClose() {}, onChanged() {},
    }));
  });

  // 1. the create surface offers the Calendar Writer capability.
  await act(async () => { byText("Create Bot").click(); });
  await act(async () => { byText("Advanced").click(); });
  const select = container.querySelector("#create-bot-preset");
  assert.ok(select, "create capability selector rendered");
  const option = [...select.querySelectorAll("option")]
    .find((o) => o.value === "calendar-writer");
  assert.ok(option, "calendar-writer create option present");
  assert.match(option.textContent, /Calendar Writer/);
  console.log("ok - create surface offers the Calendar Writer preset");

  // 2. the badge renders ONLY for the server-flagged Bot, under the write badge.
  const tags = [...container.querySelectorAll(".bot-calendar-writer-tag")]
    .map((s) => s.textContent.trim());
  assert.deepEqual(tags, ["Calendar Writer"], "exactly one writer badge");
  console.log("ok - write-capability badge renders only for the flagged Bot");

  // 3. configure labels come from the server flag and are enabled.
  assert.ok(byText("Reconfigure as Calendar Writer"),
    "flagged Bot offers Reconfigure");
  const button = byText("Configure as Calendar Writer");
  assert.ok(button, "ordinary Bot offers Configure");
  assert.equal(button.disabled, false, "enabled while the preset is present");
  console.log("ok - configure button present, enabled, flag-driven label");

  // 4. clicking opens the confirmation naming the grammar + the gate.
  await act(async () => { byText("Reconfigure as Calendar Writer").click(); });
  const dialog = container.querySelector(
    '[aria-label="Configure as Calendar Writer"]');
  assert.ok(dialog, "Calendar Writer dialog opened");
  assert.match(dialog.textContent, /create <title> on YYYY-MM-DD/);
  assert.match(dialog.textContent, /America\/New_York/);
  assert.match(dialog.textContent, /explicit approval/i);
  console.log("ok - dialog opens and shows the grammar + confirmation contract");

  console.log("all calendarWriterSettings tests passed");
}

await main();
await act(async () => { root.unmount(); });
globalThis.window.close();
process.exit(0);
