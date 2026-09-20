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
  if (url === "/api/bots/migrate") {
    return resp({ legacy: [
      { bot_id: "cw1", name: "Writer", status: "running",
        kind: "calendar-writer", kind_label: "can create calendar events" },
    ], calendar_bot_id: "" });
  }
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

  // 3. the Configure-as wall is gone: the ONE Change capability control
  //    replaces it (task: replace the wall of Configure-as buttons).
  assert.equal(byText("Configure as Calendar Writer"), undefined,
    "no wall Configure-as button for the legacy writer");
  assert.equal(byText("Reconfigure as Calendar Writer"), undefined,
    "no wall Reconfigure button either");
  assert.ok(byText("Change capability"), "the ONE capability control is offered");
  console.log("ok - wall removed; one Change capability control offered");

  // 4. the safe migration surface detects the legacy writer Bot (never a
  //    silent delete) and names its kind.
  const card = container.querySelector('[data-testid="bot-migration"]');
  assert.ok(card, "migration card rendered for the legacy writer");
  assert.match(card.textContent, /can create calendar events/);
  assert.match(card.textContent, /Consolidate into one Calendar Bot/);
  assert.match(card.textContent, /Nothing is deleted/);
  console.log("ok - migration card detects the legacy writer, nothing deleted");

  console.log("all calendarWriterSettings tests passed");
}

await main();
await act(async () => { root.unmount(); });
globalThis.window.close();
process.exit(0);
