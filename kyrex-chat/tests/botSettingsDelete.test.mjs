// botSettingsDelete.test.mjs — focused coverage for the Delete-bot confirmation
// flow's failure handling in BotSettings, rendered for real in jsdom (React 19)
// against a mock backend:
//
//   1. a successful delete POSTs { confirm_name }, closes the dialog, shows the
//      preserved-data notice, and refreshes the roster;
//   2. a backend 409 keeps the dialog OPEN, shows the exact sanitized retry
//      message, preserves the typed confirmation name, restores the button,
//      removes nothing optimistically, and exposes NO raw backend detail;
//   3. the same dialog retries and succeeds once the conflict clears;
//   4. accessibility: the conflict is announced (role="alert") and focus returns
//      to the confirm button;
//   5. a sanitized 500 is shown as-is and leaks no raw error.
//
// Run: node --import ./tests/jsdomSetup.mjs \
//        --import ./dev/jsx-loader-register.mjs --test tests/botSettingsDelete.test.mjs
import assert from "node:assert/strict";
import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import BotSettings from "../src/components/BotSettings.jsx";

const h = React.createElement;

const CONFLICT_MSG = "This bot still has work running or awaiting approval. "
  + "Finish or cancel that work, then try again.";
const RAW_409 = "RAW-BACKEND-DETAIL-9f3a2c";

const calls = [];
let deleteMode = "ok"; // "ok" | "conflict" | "server-error"

function resp(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 409 ? "Conflict"
      : status === 500 ? "Internal Server Error" : "OK",
    async json() { return body; },
  };
}

globalThis.fetch = async (url, opts = {}) => {
  const method = (opts.method || "GET").toUpperCase();
  const body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({ url, method, body });

  if (url === "/api/bots/presets") {
    return resp({ presets: [], capabilities: [
      { id: "calendar", label: "Calendar Bot", preset: "calendar",
        description: "Reads your Google calendar and creates events." }] });
  }
  if (url === "/api/chat/provider-profiles") return resp({ profiles: [] });
  if (url === "/api/chat/workspaces") return resp({ workspaces: [] });
  if (url === "/api/bots/migrate" && method === "GET") {
    return resp({ legacy: [], calendar_bot_id: "" });
  }
  if (url === "/api/connections/google/account") {
    return resp({ email: "owner@example.com", calendar_id: "primary" });
  }
  if (url === "/api/connections/google/calendars") {
    return resp({ calendars: [{ id: "primary", summary: "Owner",
                                primary: true, preferred: true }],
                  preferred: "primary" });
  }
  if (url.endsWith("/delete") && method === "POST") {
    if (deleteMode === "conflict") return resp({ detail: RAW_409 }, 409);
    if (deleteMode === "server-error") {
      return resp({ detail: "could not delete bot" }, 500);
    }
    return resp({ deleted: "calbot", name: "Cal Bot",
      preserved: ["conversations", "task_history", "google_authorization",
                  "provider_profiles", "calendar_events"] });
  }
  return resp({});
};

// ── harness ──────────────────────────────────────────────────────────────
const CAL_BOT = {
  id: "calbot", name: "Cal Bot", status: "running", model: "anthropic:claude",
  available: true, manageable: true, claimable: false, coordinator: false,
  browser_bot: false, calendar_bot: true, browser_allowlist: [],
  role: { id: "calendar", label: "Calendar Bot",
          description: "Reads your Google calendar and creates events." },
};

let onChangedCount = 0;
const container = document.createElement("div");
document.body.appendChild(container);
const root = createRoot(container);

const buttons = () => [...container.querySelectorAll("button")];
const byText = (t) => buttons().find((b) => b.textContent.trim() === t);
const moreBtn = () => container.querySelector(".bot-more-btn");
const get = (sel) => container.querySelector(sel);
const dialog = () => get('[aria-label="Delete bot"]');

function typeInto(el, value) {
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, "value").set;
  setter.call(el, value);
  el.dispatchEvent(new window.Event("input", { bubbles: true }));
}

async function openDelete() {
  await act(async () => { moreBtn().click(); });
  const item = byText("Delete bot");
  await act(async () => { item.click(); });
  return dialog();
}

async function typeNameAndConfirm(name) {
  await act(async () => { typeInto(get("#delete-confirm-calbot"), name); });
  await act(async () => { byText("Delete bot").click(); });
}

async function main() {
  await act(async () => {
    root.render(h(BotSettings, {
      bots: [CAL_BOT], onClose() {}, onChanged() { onChangedCount += 1; },
    }));
  });
  await act(async () => {});

  // ── 1. successful deletion ────────────────────────────────────────
  assert.ok(await openDelete(), "delete dialog opened");
  const before = calls.length;
  await act(async () => { typeInto(get("#delete-confirm-calbot"), "Cal Bot"); });
  assert.equal(byText("Delete bot").disabled, false, "exact name enables delete");
  await act(async () => { byText("Delete bot").click(); });
  const delCall = calls.slice(before).find((c) => c.url.endsWith("/delete"));
  assert.ok(delCall, "delete POST issued");
  assert.deepEqual(delCall.body, { confirm_name: "Cal Bot" });
  assert.equal(dialog(), null, "dialog closed on success");
  assert.match(container.textContent, /was deleted/);
  assert.ok(onChangedCount >= 1, "roster refreshed after delete");
  console.log("ok - successful deletion closes the dialog and refreshes the roster");

  // ── 2. 409: dialog stays open, name kept, no removal, no raw detail ─
  deleteMode = "conflict";
  const changedBefore = onChangedCount;
  await openDelete();
  await typeNameAndConfirm("Cal Bot");
  assert.ok(dialog(), "409 keeps the dialog OPEN");
  assert.equal(get("#delete-confirm-calbot").value, "Cal Bot",
               "typed confirmation name preserved");
  assert.match(container.textContent, /still has work running or awaiting approval/);
  assert.match(container.textContent, /Finish or cancel that work, then try again/);
  assert.ok(!container.textContent.includes(RAW_409),
            "raw backend detail is never rendered");
  assert.equal(byText("Delete bot").disabled, false,
               "confirm button restored to enabled");
  assert.equal(onChangedCount, changedBefore,
               "no optimistic removal (roster not refreshed)");
  console.log("ok - 409 keeps the dialog open, preserves the name, hides raw detail, no removal");

  // ── 3. accessibility: announced + focus restored ──────────────────
  const alert = dialog().querySelector('[role="alert"]');
  assert.ok(alert, "conflict message is announced via role=alert");
  assert.equal(alert.textContent.trim(), CONFLICT_MSG);
  assert.equal(document.activeElement, byText("Delete bot"),
               "focus restored to the confirm button");
  console.log("ok - conflict is announced and focus returns to the confirm button");

  // ── 4. retry succeeds once the conflict clears ────────────────────
  deleteMode = "ok";
  await act(async () => { byText("Delete bot").click(); });
  assert.equal(dialog(), null, "retry closes the dialog");
  assert.ok(onChangedCount > changedBefore, "roster refreshed after retry");
  console.log("ok - the same dialog retries and succeeds after the conflict clears");

  // ── 5. sanitized 500 shown as-is, no raw error ────────────────────
  deleteMode = "server-error";
  await openDelete();
  await typeNameAndConfirm("Cal Bot");
  assert.ok(dialog(), "500 keeps the dialog open");
  assert.match(container.textContent, /could not delete bot/);
  assert.ok(!container.textContent.includes("RAW-"), "no raw error detail");
  console.log("ok - a sanitized 500 is shown without raw detail");

  await act(async () => { root.unmount(); });
  globalThis.window.close();
  process.exit(0);
}

await main();
