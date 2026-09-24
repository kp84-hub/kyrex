// delegatedApprove.test.mjs — the Delegated Work card's T2 "Approve" action,
// rendered for real in jsdom (React 19).
//
// Proves the smallest safe fix at the card boundary:
//   1. a T2 Approve NAMES the task (onApproveDelegated(task_id)) — it never
//      sends a token, and it is enabled WITHOUT typing anything;
//   2. the approval TOKEN is never rendered into the DOM (the card only ever
//      shows the safe view `delegationApprovalOf` returns);
//   3. the manual-text path is UNCHANGED: typing an exact token and pressing
//      Enter still responds with that text;
//   4. the T1 Approve (y) / Deny (n) behaviour is unchanged;
//   5. a failed approve surfaces an error and stays RETRYABLE.
//
// Run (from kyrex-chat/):
//   node --import ./tests/jsdomSetup.mjs \
//        --import ./dev/jsx-loader-register.mjs \
//        --test tests/delegatedApprove.test.mjs
import assert from "node:assert/strict";
import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import DelegatedWork from "../src/components/DelegatedWork.jsx";

const h = React.createElement;
const CONV = "c-approve";
const TOKEN = "MUST-NOT-LEAK";

const T2_ROW = {
  delegation_id: "d-t2",
  target_bot_id: "calendar-editor",
  status: "awaiting_approval",
  task_id: "task-t2",
  text: "remove the workout event",
  approval: {
    task_id: "task-t2",
    tier: 2,
    summary: "delete calendar event \u201cWorkout\u201d",
    detail: "event id: abc",
    token: TOKEN,
  },
};
const T1_ROW = {
  delegation_id: "d-t1",
  target_bot_id: "dev",
  status: "awaiting_approval",
  task_id: "task-t1",
  text: "write the file",
  approval: { task_id: "task-t1", tier: 1, summary: "write", detail: "" },
};

const makeDiv = () => {
  const d = document.createElement("div");
  document.body.appendChild(d);
  return d;
};
const buttons = (scope) => [...scope.querySelectorAll("button")];
const byText = (scope, text) =>
  buttons(scope).find((b) => b.textContent.trim() === text);
const input = (scope) => scope.querySelector(".approval-input");

function typeInto(el, value) {
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, "value").set;
  setter.call(el, value);
  el.dispatchEvent(new window.Event("input", { bubbles: true }));
}

function pressEnter(el) {
  el.dispatchEvent(new window.KeyboardEvent("keydown", {
    key: "Enter", bubbles: true,
  }));
}

async function mount(container, rows, props = {}) {
  const root = createRoot(container);
  await act(async () => {
    root.render(h(DelegatedWork, {
      delegations: rows, conversationId: CONV, ...props,
    }));
  });
  return root;
}

async function main() {
  globalThis.localStorage = globalThis.window.localStorage;
  window.localStorage.clear();

  // ── 1 + 2. T2 Approve names the task, needs no token, leaks no token ──
  {
    const c = makeDiv();
    const approved = [];
    const responded = [];
    const root = await mount(c, [T2_ROW], {
      onApproveDelegated: async (taskId) => { approved.push(taskId); },
      onRespondApproval: async (taskId, text) => { responded.push([taskId, text]); },
    });

    assert.ok(!c.innerHTML.includes(TOKEN),
              "the approval token is never rendered into the DOM");

    const approve = byText(c, "Approve");
    assert.ok(approve, "the T2 card offers Approve");
    assert.equal(approve.disabled, false,
                 "Approve is enabled WITHOUT typing a token");

    await act(async () => { approve.click(); });
    assert.deepEqual(approved, ["task-t2"],
                     "Approve resolves exactly the row's task id");
    assert.deepEqual(responded, [],
                     "Approve never sends a token/text to the manual path");
    console.log("ok - T2 Approve names the task, needs no token, leaks no token");
    await act(async () => { root.unmount(); });
    c.remove();
  }

  // ── 3. manual-text path unchanged: token + Enter -> respond ──────────
  {
    const c = makeDiv();
    const approved = [];
    const responded = [];
    const root = await mount(c, [T2_ROW], {
      onApproveDelegated: async (taskId) => { approved.push(taskId); },
      onRespondApproval: async (taskId, text) => { responded.push([taskId, text]); },
    });

    await act(async () => { typeInto(input(c), "  manual-token  "); });
    await act(async () => { pressEnter(input(c)); });
    assert.deepEqual(responded, [["task-t2", "manual-token"]],
                     "manual text still responds with the trimmed value");
    assert.deepEqual(approved, [], "manual text does not trigger the approve action");
    console.log("ok - the manual-text (exact token) path is unchanged");
    await act(async () => { root.unmount(); });
    c.remove();
  }

  // ── 4. T1 y/n behaviour unchanged ────────────────────────────────────
  {
    const c = makeDiv();
    const responded = [];
    const root = await mount(c, [T1_ROW], {
      onRespondApproval: async (taskId, text) => { responded.push([taskId, text]); },
    });

    await act(async () => { byText(c, "Approve (y)").click(); });
    await act(async () => { byText(c, "Deny (n)").click(); });
    assert.deepEqual(responded, [["task-t1", "y"], ["task-t1", "n"]],
                     "T1 Approve(y)/Deny(n) are unchanged");
    console.log("ok - T1 Approve(y)/Deny(n) unchanged");
    await act(async () => { root.unmount(); });
    c.remove();
  }

  // ── 5. a failed approve is surfaced and stays retryable ──────────────
  {
    const c = makeDiv();
    let attempts = 0;
    let failNext = true;
    const root = await mount(c, [T2_ROW], {
      onApproveDelegated: async () => {
        attempts += 1;
        if (failNext) { failNext = false; throw new Error("Could not approve"); }
      },
      onRespondApproval: async () => {},
    });

    await act(async () => { byText(c, "Approve").click(); });
    assert.match(c.textContent, /Could not approve/,
                 "a failed approve surfaces a sanitized error");
    assert.equal(byText(c, "Approve").disabled, false,
                 "Approve is retryable after a failure");

    await act(async () => { byText(c, "Approve").click(); });
    assert.equal(attempts, 2, "the retry reaches the approve action again");
    console.log("ok - a failed approve surfaces an error and stays retryable");
    await act(async () => { root.unmount(); });
    c.remove();
  }

  console.log("ok - delegated T2 approve (host-side) verified");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});