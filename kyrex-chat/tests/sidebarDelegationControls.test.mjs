// sidebarDelegationControls.test.mjs — the Delegated Work card's "Done"
// dismissal, rendered for real in jsdom (React 19).
//
// Proves the completed-work acknowledgement:
//   1. ONLY a done row carries the "Done" control — running/failed work can
//      never be hidden;
//   2. acknowledging a done row removes it and records ONLY that delegation id,
//      under the per-conversation key
//      `kyrex:delegated-work-dismissed:v1:<conversationId>`;
//   3. RECREATING the card after a refresh (fresh mount, same localStorage)
//      keeps the row dismissed — the acknowledgement survives;
//   4. a different conversation is unaffected (per-conversation isolation).
//
// Run (from kyrex-chat/):
//   node --import ./tests/jsdomSetup.mjs \
//        --import ./dev/jsx-loader-register.mjs \
//        tests/sidebarDelegationControls.test.mjs
import assert from "node:assert/strict";
import React from "react";
import { createRoot } from "react-dom/client";
import { act } from "react";
import DelegatedWork from "../src/components/DelegatedWork.jsx";
import {
  DELEGATED_WORK_DISMISSED_PREFIX,
  delegatedWorkDismissKey,
  isDismissibleDelegation,
  readDismissedDelegations,
  writeDismissedDelegations,
  withDismissedDelegation,
  visibleDelegations,
  defaultDelegatedWorkStorage,
} from "../src/lib/delegations.js";

const h = React.createElement;

// A Map backed Web Storage stand-in for the pure-helper checks.
function mapStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => { m.set(k, String(v)); },
    removeItem: (k) => { m.delete(k); },
  };
}

// ── 1. per-conversation key ────────────────────────────────────────────
{
  assert.equal(
    DELEGATED_WORK_DISMISSED_PREFIX,
    "kyrex:delegated-work-dismissed:v1:",
  );
  assert.equal(
    delegatedWorkDismissKey("conv-A"),
    "kyrex:delegated-work-dismissed:v1:conv-A",
  );
  console.log("ok - per-conversation dismissal key");
}

// ── 2. only done work is dismissible ───────────────────────────────────
{
  assert.ok(isDismissibleDelegation("done"));
  for (const s of [
    "queued", "running", "awaiting_approval", "failed", "cancelled", "rejected",
  ]) {
    assert.ok(!isDismissibleDelegation(s), `${s} must NOT be dismissible`);
  }
  console.log("ok - only done work is dismissible");
}

// ── 3. the only-done invariant is enforced when recording ──────────────
{
  const store = mapStorage();
  let ids = new Set();
  ids = withDismissedDelegation(ids, { delegation_id: "d-run", status: "running" });
  assert.equal(ids.size, 0, "a running row is never recorded");
  ids = withDismissedDelegation(ids, { delegation_id: "d-done", status: "done" });
  assert.deepEqual([...ids], ["d-done"]);
  writeDismissedDelegations("conv-A", ids, store);
  assert.deepEqual(
    JSON.parse(store.getItem(delegatedWorkDismissKey("conv-A"))),
    ["d-done"],
  );
  assert.deepEqual([...readDismissedDelegations("conv-A", store)], ["d-done"]);
  console.log("ok - only done delegation ids are recorded");
}

// ── 4. safe localStorage: unavailable / malformed never throw ──────────
{
  assert.deepEqual([...readDismissedDelegations("conv-A", null)], []);
  assert.deepEqual([...readDismissedDelegations("", mapStorage())], []);
  assert.deepEqual(
    [...readDismissedDelegations("conv-A", { getItem: () => "{not json" })],
    [],
  );
  assert.deepEqual(
    [...readDismissedDelegations("conv-A", { getItem: () => '{"x":1}' })],
    [],
  );
  const throwing = {
    getItem() { throw new Error("blocked"); },
    setItem() { throw new Error("blocked"); },
  };
  assert.deepEqual([...readDismissedDelegations("conv-A", throwing)], []);
  writeDismissedDelegations("conv-A", new Set(["d-done"]), throwing); // no throw
  writeDismissedDelegations(null, new Set(["d-done"]), mapStorage());  // no throw
  assert.ok(defaultDelegatedWorkStorage(), "jsdom provides localStorage");
  console.log("ok - safe localStorage degrades without throwing");
}

// ── 5. a stored id only hides a row that is STILL done ─────────────────
{
  const rows = [
    { delegation_id: "d-done", status: "done" },
    { delegation_id: "d-run", status: "running" },
  ];
  assert.deepEqual(
    visibleDelegations(rows, new Set(["d-done"])).map((r) => r.delegation_id),
    ["d-run"],
  );
  // Same id, but the row regressed to non-terminal -> it reappears.
  assert.deepEqual(
    visibleDelegations(
      [{ delegation_id: "d-done", status: "running" }],
      new Set(["d-done"]),
    ).map((r) => r.delegation_id),
    ["d-done"],
  );
  console.log("ok - dismissal never masks live work");
}

// ── 6. rendered card: dismiss, then RECREATE after a refresh ───────────
const DONE_ROW = {
  delegation_id: "del-done-1",
  target_bot_id: "bot-a",
  status: "done",
  text: "daily report",
  result_summary: "Report ready.",
};
const RUN_ROW = {
  delegation_id: "del-run-1",
  target_bot_id: "bot-b",
  status: "running",
  text: "scan site",
};
const ROWS = [DONE_ROW, RUN_ROW];
const CONV = "conv-refresh";

async function mount(conversationId, container) {
  const root = createRoot(container);
  await act(async () => {
    root.render(h(DelegatedWork, { delegations: ROWS, conversationId }));
  });
  return root;
}

const items = (container) =>
  [...container.querySelectorAll(".delegated-work-item")];
const showsTarget = (container, id) =>
  items(container).some((li) => li.textContent.includes(id));
const doneButton = () =>
  [...document.querySelectorAll("button.delegated-work-dismiss")].find(
    (b) => b.textContent.trim() === "Done",
  );

async function main() {
  // Browser-ish storage shared across mounts (jsdom's), cleared per run.
  globalThis.localStorage = globalThis.window.localStorage;
  window.localStorage.clear();

  const c1 = document.createElement("div");
  document.body.appendChild(c1);
  const root1 = await mount(CONV, c1);

  // Both rows render; only the done row offers "Done".
  assert.ok(showsTarget(c1, "bot-a"), "done row rendered");
  assert.ok(showsTarget(c1, "bot-b"), "running row rendered");
  assert.ok(doneButton(), "the done row has a Done control");
  assert.equal(
    document.querySelectorAll("button.delegated-work-dismiss").length,
    1,
    "only the done row is dismissible",
  );
  console.log("ok - only the done row offers Done");

  // Acknowledge the done row.
  await act(async () => { doneButton().click(); });
  assert.ok(!showsTarget(c1, "bot-a"), "dismissed done row is gone");
  assert.ok(showsTarget(c1, "bot-b"), "running row is untouched");
  assert.equal(
    document.querySelectorAll("button.delegated-work-dismiss").length,
    0,
    "no Done control remains",
  );
  // ONLY the done id is persisted, under the per-conversation key.
  assert.deepEqual(
    JSON.parse(window.localStorage.getItem(delegatedWorkDismissKey(CONV))),
    ["del-done-1"],
  );
  console.log("ok - Done records only the done delegation id");

  // REFRESH RECREATION: unmount, then recreate the card fresh from storage.
  await act(async () => { root1.unmount(); });
  const c2 = document.createElement("div");
  document.body.appendChild(c2);
  const root2 = await mount(CONV, c2);
  assert.ok(
    !showsTarget(c2, "bot-a"),
    "dismissed done row stays dismissed after a refresh",
  );
  assert.ok(showsTarget(c2, "bot-b"), "running row still shows after refresh");
  console.log("ok - dismissal survives card recreation (refresh)");

  // Per-conversation isolation.
  const c3 = document.createElement("div");
  document.body.appendChild(c3);
  const root3 = await mount("conv-other", c3);
  assert.ok(
    showsTarget(c3, "bot-a"),
    "a different conversation is unaffected by this dismissal",
  );
  console.log("ok - dismissals are per conversation");

  await act(async () => { root2.unmount(); });
  await act(async () => { root3.unmount(); });

  console.log("all sidebarDelegationControls tests passed");
}

await main();

// Tear down so the process exits 0 (jsdom timers would keep it alive).
globalThis.window.close();
process.exit(0);
