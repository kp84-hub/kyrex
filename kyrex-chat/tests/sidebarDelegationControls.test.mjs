import assert from "node:assert/strict";
import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import Sidebar from "../src/components/Sidebar.jsx";
import DelegatedWork from "../src/components/DelegatedWork.jsx";

const h = React.createElement;
const container = document.createElement("div");
document.body.appendChild(container);
const root = createRoot(container);

async function main() {
  await act(async () => {
    root.render(h(Sidebar, {
      conversations: [{
        conversation_id: "c1",
        bot_id: "chief",
        title: "Chief of Staff",
        latest_update: "Your calendar has three events today.",
        updated_at: new Date().toISOString(),
      }],
      activeId: "c1",
      bots: [{ id: "chief", name: "Chief of Staff", status: "running" }],
      activityLines: {},
      onSelect() {}, onNew() {}, onDelete() {}, onSettings() {}, onBots() {},
      open: true,
    }));
  });
  assert.equal(
    container.querySelector(".conversation-subtitle").textContent,
    "Your calendar has three events today.",
    "latest conversation update replaces the generic Ready label"
  );

  await act(async () => {
    root.render(h(DelegatedWork, {
      delegations: [{
        delegation_id: "d1",
        target_bot_id: "calendar",
        status: "done",
        text: "calendar: today",
        result_summary: "Three events",
      }],
    }));
  });
  const done = [...container.querySelectorAll("button")]
    .find((button) => button.textContent.trim() === "Done");
  assert.ok(done, "Done is an actionable button");
  assert.match(done.getAttribute("aria-label"), /Dismiss completed calendar delegation/);
  await act(async () => { done.click(); });
  assert.equal(container.querySelector(".delegated-work"), null,
               "Done dismisses the completed delegated-work card");

  await act(async () => { root.unmount(); });
  container.remove();
  console.log("ok - sidebar preview and delegated Done dismissal");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
