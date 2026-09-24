// sidebarDelegationControls.test.mjs — the Delegated Work card's Done/Close
// terminal dismissal, rendered for real in jsdom (React 19).
//
// Proves:
//   1. the sidebar shows the latest conversation update (unchanged);
//   2. a TERMINAL row is dismissible (Done for done, Close otherwise), and the
//      control removes the terminal card;
//   3. the acknowledgement is PERSISTED per conversation under
//      `kyrex:delegated-work-dismissed:v1:<conversationId>`, recording ONLY a
//      terminal id;
//   4. RECREATING the card after a refresh keeps the dismissed row gone while
//      still-live work is shown — the dismissal survives;
//   5. failed / cancelled / rejected rows each offer Close and stay hidden
//      after a refresh, while non-terminal work can NOT be dismissed;
//   6. a different conversation is unaffected (per-conversation isolation).
//
// Run (from kyrex-chat/):
//   node --import ./tests/jsdomSetup.mjs \
//        --import ./dev/jsx-loader-register.mjs \
//        tests/sidebarDelegationControls.test.mjs
import assert from "node:assert/strict";
import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import Sidebar from "../src/components/Sidebar.jsx";
import DelegatedWork from "../src/components/DelegatedWork.jsx";
import {
  delegatedWorkDismissKey,
  withDismissedDelegation,
  visibleDelegations,
  readDismissedDelegations,
  writeDismissedDelegations,
} from "../src/lib/delegations.js";

const h = React.createElement;
const CONV = "c1";
const DONE_ROW = {
  delegation_id: "d1",
  target_bot_id: "calendar",
  status: "done",
  text: "calendar: today",
  result_summary: "Three events",
};
const RUN_ROW = {
  delegation_id: "d2",
  target_bot_id: "calendar",
  status: "running",
  task_id: "task-running-1",
  text: "sync site",
};
// One row per non-done terminal status, each with distinct task text so the
// rendered card can be identified precisely.
const FAILED_ROW = {
  delegation_id: "d3",
  target_bot_id: "calendar",
  status: "failed",
  text: "failed job",
  error: "boom",
};
const CANCELLED_ROW = {
  delegation_id: "d4",
  target_bot_id: "calendar",
  status: "cancelled",
  text: "cancelled job",
};
const REJECTED_ROW = {
  delegation_id: "d5",
  target_bot_id: "calendar",
  status: "rejected",
  text: "rejected job",
  error: "refused",
};
const RUN_JOB_ROW = {
  delegation_id: "d6",
  target_bot_id: "calendar",
  status: "running",
  task_id: "task-running-2",
  text: "running job",
};

async function mountRows(container, rows, conversationId, props = {}) {
  const root = createRoot(container);
  await act(async () => {
    root.render(h(DelegatedWork, {
      delegations: rows,
      conversationId,
      ...props,
    }));
  });
  return root;
}

const makeDiv = () => {
  const d = document.createElement("div");
  document.body.appendChild(d);
  return d;
};
const doneButton = (scope) =>
  [...scope.querySelectorAll("button")]
    .find((button) => button.textContent.trim() === "Done");
const hasText = (container, text) =>
  [...container.querySelectorAll(".delegated-work-item")]
    .some((li) => li.textContent.includes(text));
// The dismiss control rendered inside the row that carries *text*, or null.
const dismissControlIn = (container, text) => {
  const li = [...container.querySelectorAll(".delegated-work-item")]
    .find((node) => node.textContent.includes(text));
  if (!li) return null;
  return [...li.querySelectorAll("button")]
    .find((button) => ["Done", "Close"].includes(button.textContent.trim())) || null;
};

// ── pure helpers: only-done invariant + safe storage ────────────────────
{
  let ids = withDismissedDelegation(new Set(), { delegation_id: "d2", status: "running" });
  assert.equal(ids.size, 0, "a running row is never recorded");
  ids = withDismissedDelegation(ids, DONE_ROW);
  assert.deepEqual([...ids], ["d1"]);
  assert.deepEqual(
    visibleDelegations([DONE_ROW, RUN_ROW], new Set(["d1"]))
      .map((r) => r.delegation_id),
    ["d2"],
    "a dismissed done row is hidden; live work is not",
  );
  // Every TERMINAL status is recorded; no non-terminal status ever is.
  for (const row of [DONE_ROW, FAILED_ROW, CANCELLED_ROW, REJECTED_ROW]) {
    assert.ok(
      withDismissedDelegation(new Set(), row).has(row.delegation_id),
      `terminal ${row.status} is dismissible`,
    );
  }
  for (const row of [RUN_ROW, { delegation_id: "q", status: "queued" },
                     { delegation_id: "a", status: "awaiting_approval" }]) {
    assert.equal(
      withDismissedDelegation(new Set(), row).size, 0,
      `non-terminal ${row.status} is never recorded`,
    );
  }
  // A stored terminal id hides that row; it never hides a live/regressed row.
  assert.deepEqual(
    visibleDelegations([FAILED_ROW, RUN_ROW], new Set(["d3"]))
      .map((r) => r.delegation_id),
    ["d2"],
    "a dismissed failed row is hidden; live work is not",
  );
  assert.deepEqual(
    visibleDelegations([RUN_ROW], new Set(["d2"])).map((r) => r.delegation_id),
    ["d2"],
    "a stored id never hides a non-terminal (regressed) row",
  );
  assert.deepEqual([...readDismissedDelegations("c1", null)], []);
  const throwing = {
    getItem() { throw new Error("blocked"); },
    setItem() { throw new Error("blocked"); },
  };
  assert.deepEqual([...readDismissedDelegations("c1", throwing)], []);
  writeDismissedDelegations("c1", new Set(["d1"]), throwing); // must not throw
  console.log("ok - only terminal work is ever hidden; storage access is safe");
}

async function main() {
  // Browser-ish storage shared across mounts (jsdom's), cleared per run.
  globalThis.localStorage = globalThis.window.localStorage;
  window.localStorage.clear();

  // 1. Sidebar latest-update preview (unchanged behaviour).
  const side = makeDiv();
  const sideRoot = createRoot(side);
  await act(async () => {
    sideRoot.render(h(Sidebar, {
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
    side.querySelector(".conversation-subtitle").textContent,
    "Your calendar has three events today.",
    "latest conversation update replaces the generic Ready label",
  );
  await act(async () => { sideRoot.unmount(); });
  side.remove();
  console.log("ok - sidebar preview shows the latest conversation update");

  // 2. Done is actionable and dismisses the completed card.
  const c1 = makeDiv();
  const root1 = await mountRows(c1, [DONE_ROW], CONV);
  const done = doneButton(c1);
  assert.ok(done, "Done is an actionable button");
  assert.match(done.getAttribute("aria-label"), /Dismiss done calendar delegation/);
  await act(async () => { done.click(); });
  assert.equal(c1.querySelector(".delegated-work"), null,
               "Done dismisses the completed delegated-work card");
  console.log("ok - delegated Done dismissal");

  // 3. The dismissal is PERSISTED for this conversation only, and ONLY the
  //    done id is recorded.
  assert.deepEqual(
    JSON.parse(window.localStorage.getItem(delegatedWorkDismissKey(CONV))),
    ["d1"],
    "only the done delegation id is persisted",
  );
  assert.equal(
    window.localStorage.getItem(delegatedWorkDismissKey("other")),
    null,
    "nothing is persisted for another conversation",
  );
  console.log("ok - only the done id is persisted, under the per-conversation key");

  // 4. REFRESH RECREATION: a fresh mount reads the store, so the completed
  //    card stays dismissed while still-live work shows.
  const c2 = makeDiv();
  const root2 = await mountRows(c2, [DONE_ROW, RUN_ROW], CONV);
  assert.ok(!hasText(c2, "calendar: today"),
            "dismissed completed row stays gone after a refresh");
  assert.ok(hasText(c2, "sync site"),
            "still-running work is shown after a refresh");
  console.log("ok - dismissed completed work survives a refresh recreation");
  await act(async () => { root2.unmount(); });

  // 5. Active delegated work exposes a task-scoped Cancel action.
  const cCancel = makeDiv();
  const cancelled = [];
  const cancelRoot = await mountRows(cCancel, [RUN_ROW], CONV, {
    onCancelTask: async (taskId) => { cancelled.push(taskId); },
  });
  const cancelButton = [...cCancel.querySelectorAll("button")]
    .find((button) => button.textContent.trim() === "Cancel task");
  assert.ok(cancelButton, "running delegated work exposes Cancel task");
  await act(async () => { cancelButton.click(); });
  assert.deepEqual(cancelled, ["task-running-1"],
                   "Cancel is scoped to the row's durable task id");
  await act(async () => { cancelRoot.unmount(); });
  cCancel.remove();
  console.log("ok - active delegated work can cancel its durable task");

  // 6. A different conversation is unaffected by this conversation's dismissal.
  const c3 = makeDiv();
  const root3 = await mountRows(c3, [DONE_ROW], "other");
  assert.ok(doneButton(c3), "another conversation still offers Done");
  assert.ok(hasText(c3, "calendar: today"),
            "another conversation's completed card is not dismissed");
  console.log("ok - dismissals are per conversation");

  // 7. Terminal NON-done rows (failed / cancelled / rejected) each render an
  //    actionable Close; non-terminal work exposes no dismiss control at all.
  const CONV_T = "cterm";
  window.localStorage.removeItem(delegatedWorkDismissKey(CONV_T));
  const cT = makeDiv();
  const tRoot = await mountRows(
    cT, [FAILED_ROW, CANCELLED_ROW, REJECTED_ROW, RUN_JOB_ROW], CONV_T);
  const closeButtons = [...cT.querySelectorAll("button")]
    .filter((button) => button.textContent.trim() === "Close");
  assert.equal(closeButtons.length, 3,
               "failed / cancelled / rejected each expose a Close control");
  assert.equal(dismissControlIn(cT, "running job"), null,
               "running work is not dismissible");
  // Dismiss the failed row: it hides immediately and its id is persisted.
  await act(async () => { dismissControlIn(cT, "failed job").click(); });
  assert.ok(!hasText(cT, "failed job"), "failed row hides on Close");
  assert.ok(
    JSON.parse(window.localStorage.getItem(delegatedWorkDismissKey(CONV_T)))
      .includes("d3"),
    "the dismissed failed id is persisted",
  );
  await act(async () => { tRoot.unmount(); });
  cT.remove();
  console.log("ok - failed/cancelled/rejected offer Close; running is not dismissible");

  // 8. REFRESH: a dismissed terminal row stays hidden, while undismissed
  //    terminal rows and live work still render.
  const cT2 = makeDiv();
  const tRoot2 = await mountRows(
    cT2, [FAILED_ROW, CANCELLED_ROW, REJECTED_ROW, RUN_JOB_ROW], CONV_T);
  assert.ok(!hasText(cT2, "failed job"),
            "dismissed failed row stays hidden after a refresh");
  assert.ok(hasText(cT2, "cancelled job") && hasText(cT2, "rejected job"),
            "undismissed terminal rows still render after a refresh");
  assert.ok(hasText(cT2, "running job"),
            "live work still renders after a refresh");
  // Dismiss the remaining terminal rows (separate events, as a user would),
  // then refresh once more.
  await act(async () => { dismissControlIn(cT2, "cancelled job").click(); });
  await act(async () => { dismissControlIn(cT2, "rejected job").click(); });
  await act(async () => { tRoot2.unmount(); });
  cT2.remove();
  const cT3 = makeDiv();
  const tRoot3 = await mountRows(
    cT3, [FAILED_ROW, CANCELLED_ROW, REJECTED_ROW, RUN_JOB_ROW], CONV_T);
  assert.ok(!hasText(cT3, "failed job")
            && !hasText(cT3, "cancelled job")
            && !hasText(cT3, "rejected job"),
            "every dismissed terminal row stays hidden after a refresh");
  assert.ok(hasText(cT3, "running job"),
            "only live work remains after dismissing every terminal row");
  await act(async () => { tRoot3.unmount(); });
  cT3.remove();
  console.log("ok - terminal dismissals persist across refresh; live work survives");

  await act(async () => { root3.unmount(); });
  await act(async () => { root1.unmount(); });
  c1.remove(); c2.remove(); c3.remove();

  console.log("ok - sidebar preview and delegated terminal Done/Close dismissal (persisted)");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
