// delegations.test.mjs — the Delegated Work card's refresh decision.
//
// Proves the card keeps refreshing while delegated work is non-terminal and
// STOPS the moment every row is terminal (no idle refresh loop), and that the
// status set matches the backend's delegation lifecycle.
//
// Run: node tests/delegations.test.mjs
import assert from "node:assert/strict";
import {
  isTerminalDelegation,
  delegationsNeedPolling,
  TERMINAL_DELEGATION_STATUSES,
} from "../src/lib/delegations.js";

// ── 1. terminal status set ─────────────────────────────────────────
{
  for (const s of ["done", "failed", "cancelled", "rejected"]) {
    assert.ok(isTerminalDelegation(s), `${s} must be terminal`);
  }
  for (const s of ["queued", "running", "awaiting_approval", "unknown"]) {
    assert.ok(!isTerminalDelegation(s), `${s} must NOT be terminal`);
  }
  assert.deepEqual(
    [...TERMINAL_DELEGATION_STATUSES].sort(),
    ["cancelled", "done", "failed", "rejected"]
  );
}

// ── 2. keep refreshing while non-terminal ──────────────────────────
{
  assert.ok(delegationsNeedPolling([{ status: "queued" }]));
  assert.ok(delegationsNeedPolling([{ status: "running" }]));
  assert.ok(delegationsNeedPolling([{ status: "awaiting_approval" }]));
  // One finished row must not stop polling while another still runs.
  assert.ok(delegationsNeedPolling([
    { status: "done" },
    { status: "running" },
  ]));
  // A target awaiting the owner's approval keeps refreshing.
  assert.ok(delegationsNeedPolling([
    { status: "done" },
    { status: "awaiting_approval" },
  ]));
}

// ── 3. STOP once terminal (and when empty) ─────────────────────────
{
  assert.ok(!delegationsNeedPolling([{ status: "done" }]));
  assert.ok(!delegationsNeedPolling([{ status: "failed" }, { status: "cancelled" }]));
  assert.ok(!delegationsNeedPolling([{ status: "rejected" }]));
  assert.ok(!delegationsNeedPolling([]), "no work => no polling");
  assert.ok(!delegationsNeedPolling(undefined), "defensive: no rows => no polling");
}

// ── 4. malformed rows never crash the loop ─────────────────────────
{
  assert.ok(!delegationsNeedPolling([null, undefined]), "null rows are inert");
  assert.ok(delegationsNeedPolling([{ status: undefined }, { status: "running" }]));
}

console.log("✓ delegated work: bounded refresh + terminal stop verified.");

test("delegated approval helper exposes only safe task-scoped fields", () => {
  assert.deepEqual(delegationApprovalOf({
    status: "awaiting_approval",
    approval: {
      task_id: "task-1", tier: 1, summary: "Create event",
      detail: "19:30-19:45", token: "MUST-NOT-LEAK",
    },
  }), {
    task_id: "task-1", tier: 1, summary: "Create event",
    detail: "19:30-19:45",
  });
  assert.equal(delegationApprovalOf({ status: "running" }), null);
  assert.equal(delegationApprovalOf({
    status: "awaiting_approval", approval: { task_id: "task-2", tier: 9 },
  }), null);
});
