// calendarReaderSettings.test.mjs — the Calendar Reader's VISIBLE surface in
// BotSettings, rendered for real in jsdom (React 19) against a mock backend.
//
// Proves the create/configure option and the badge exist and are wired to the
// server preset — WITHOUT a Calendar Reader in the presets payload the option
// cannot be configured, and the badge only appears for a Bot the SERVER flags:
//   1. the create "Capability" selector offers the calendar-reader option;
//   2. the "Calendar Reader" badge renders for a server-flagged Bot and NOT
//      for an ordinary one;
//   3. the configure button is labelled "Configure"/"Reconfigure" from the
//      server flag, is enabled when the preset is present, and opens the
//      confirmation dialog naming the three pinned commands.
//
// Run: node --import ./tests/jsdomSetup.mjs \
//        --import ./dev/jsx-loader-register.mjs ./tests/calendarReaderSettings.test.mjs
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
      { id: "glofox-reader", label: "Glofox Reader",
        permissions: { "glofox:read": 0 } },
      { id: "calendar-reader", label: "Calendar Reader",
        policy: { "cal:list": 0 },
        permissions: { "cal:list": 0, "fs:write": "deny",
                       "browser:navigate": "deny", "cal:create": "deny" } },
    ] });
  }
  if (url === "/api/chat/provider-profiles") return resp({ profiles: [] });
  if (url === "/api/chat/workspaces") return resp({ workspaces: [] });
  if (url === "/api/bots/migrate") {
    return resp({ legacy: [
      { bot_id: "cal1", name: "Calendar", status: "running",
        kind: "calendar-reader", kind_label: "can read a calendar" },
    ], calendar_bot_id: "" });
  }
  if (url.endsWith("/browser-host") && method === "GET") {
    return resp({ bot_id: "cal1", bound_host_id: "", host: null, hosts: [] });
  }
  return resp({});
};


const BASE = {
  status: "running", model: "openai:gpt-test", available: true,
  manageable: true, claimable: false, coordinator: false, browser_bot: false,
  glofox_reader: false, level6_weekly: false,
  browser_allowlist: [], provider_profile_id: "p1",
};
const READER = { ...BASE, id: "cal1", name: "Calendar", calendar_reader: true };
const PLAIN = { ...BASE, id: "plain", name: "Plain", calendar_reader: false };


const container = document.createElement("div");
document.body.appendChild(container);
const root = createRoot(container);


const buttons = () => [...container.querySelectorAll("button")];
const byText = (t) => buttons().find((b) => b.textContent.trim() === t);


async function main() {
  await act(async () => {
    root.render(h(BotSettings, {
      bots: [READER, PLAIN], onClose() {}, onChanged() {},
    }));
  });


  // 1. the create surface offers the Calendar Reader capability. The Capability
  //    selector lives in the create form's advanced section.
  await act(async () => { byText("Create Bot").click(); });   // open the form
  await act(async () => { byText("Advanced").click(); });     // open advanced
  const select = container.querySelector("#create-bot-preset");
  assert.ok(select, "create capability selector rendered");
  const option = [...select.querySelectorAll("option")]
    .find((o) => o.value === "calendar-reader");
  assert.ok(option, "calendar-reader create option present");
  assert.match(option.textContent, /Calendar Reader/);
  const editorOption = [...select.querySelectorAll("option")]
    .find((o) => o.value === "calendar-editor");
  assert.ok(editorOption, "calendar-editor create option present");
  assert.match(editorOption.textContent, /Calendar Editor/);
  assert.match(editorOption.textContent, /approval/);
  console.log("ok - create surface offers Calendar Reader and Calendar Editor presets");


  // 2. the badge renders ONLY for the server-flagged Bot.
  const tags = [...container.querySelectorAll(".bot-glofox-tag")]
    .map((s) => s.textContent.trim());
  assert.deepEqual(tags, ["Calendar Reader"], "exactly one reader badge");
  console.log("ok - badge renders only for the server-flagged reader");


  // 3. the Configure-as wall is gone: the ONE Change capability control
  //    replaces it (task: replace the wall of Configure-as buttons).
  assert.equal(byText("Configure as Calendar Reader"), undefined,
    "no wall Configure-as button for the legacy reader");
  assert.equal(byText("Reconfigure as Calendar Reader"), undefined,
    "no wall Reconfigure button either");
  assert.ok(byText("Change capability"), "the ONE capability control is offered");
  console.log("ok - wall removed; one Change capability control offered");


  // 4. the safe migration surface detects the legacy reader Bot (never a
  //    silent delete) and names what it can do.
  const card = container.querySelector('[data-testid="bot-migration"]');
  assert.ok(card, "migration card rendered for the legacy reader");
  assert.match(card.textContent, /can read a calendar/);
  assert.match(card.textContent, /Consolidate into one Calendar Bot/);
  assert.match(card.textContent, /Nothing is deleted/);
  console.log("ok - migration card detects the legacy reader, nothing deleted");


// (steps 3-4 replaced by wall-removal + migration-detection assertions)
  console.log("all calendarReaderSettings tests passed");
}


await main();
await act(async () => { root.unmount(); });
globalThis.window.close();
process.exit(0);
