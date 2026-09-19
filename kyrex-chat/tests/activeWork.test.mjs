// activeWork.test.mjs — the sidebar active-work line derivation.
//
// Proves the line is derived ONLY from durable task/delegation state (never
// model output): terminal work clears it, a delegation reads as
// "Delegating …"/"Waiting for <Bot>", an ordinary Bot task reads with its own
// verb conjugated, it stays to a single truncated line, and a live (SSE/Flux)
// overlay wins over the durable descriptor.
//
// Run: node tests/activeWork.test.mjs
import assert from "node:assert/strict";
import {
  activityLine,
  activeSubscriptions,
  botDisplayName,
  buildActivityLines,
  humanizeBotId,
  isActiveActivity,
  isTerminalActivity,
  truncateLine,
  TERMINAL_ACTIVITY_STATUSES,
} from "../src/lib/activeWork.js";

// ── 1. terminal vs active status sets ──────────────────────────────
{
  for (const s of ["done", "failed", "cancelled", "rejected"]) {
    assert.ok(isTerminalActivity(s), `${s} must be terminal`);
    assert.ok(!isActiveActivity(s), `${s} must not be active`);
  }
  for (const s of ["queued", "running", "awaiting_approval"]) {
    assert.ok(isActiveActivity(s), `${s} must be active`);
    assert.ok(!isTerminalActivity(s), `${s} must not be terminal`);
  }
  assert.ok(!isActiveActivity("unknown"));
  assert.deepEqual(
    [...TERMINAL_ACTIVITY_STATUSES].sort(),
    ["cancelled", "done", "failed", "rejected"]
  );
}

// ── 2. nothing is shown for terminal / absent work ─────────────────
{
  assert.equal(activityLine(null), null);
  assert.equal(activityLine(undefined), null);
  assert.equal(activityLine({ kind: "task", status: "done", text: "x" }), null);
  assert.equal(activityLine({ kind: "task", status: "failed" }), null);
  assert.equal(activityLine({ kind: "delegation", status: "cancelled" }), null);
  assert.equal(activityLine({ kind: "delegation", status: "rejected" }), null);
  assert.equal(activityLine({ kind: "task", status: "unknown" }), null);
  assert.equal(activityLine("not an object"), null);
}

// ── 3. delegations: "Delegating …" then "Waiting for <Bot>" ────────
{
  const bots = [{ id: "calendar-reader", name: "Calendar Reader" }];

  // Freshly queued delegation → what we are delegating.
  assert.equal(
    activityLine(
      { kind: "delegation", status: "queued", text: "Read this week's calendar" },
      { bots }
    ),
    "Delegating this week's calendar"
  );
  // A bare noun phrase is kept verbatim.
  assert.equal(
    activityLine({ kind: "delegation", status: "queued", text: "calendar request." }, { bots }),
    "Delegating calendar request"
  );
  // In flight → the target Bot we are waiting on (registry name).
  assert.equal(
    activityLine(
      { kind: "delegation", status: "running", target_bot_id: "calendar-reader" },
      { bots }
    ),
    "Waiting for Calendar Reader"
  );
  // Unknown Bot id → a humanized slug (still never invented).
  assert.equal(
    activityLine({ kind: "delegation", status: "awaiting_approval", target_bot_id: "workout-finder" }),
    "Waiting for Workout Finder"
  );
  // kind may be inferred from the presence of a target Bot.
  assert.equal(
    activityLine({ status: "running", target_bot_id: "calendar-reader" }, { bots }),
    "Waiting for Calendar Reader"
  );
}

// ── 4. ordinary Bot tasks read with their own verb conjugated ──────
{
  assert.equal(
    activityLine({ kind: "task", status: "running", text: "Read this week's calendar" }),
    "Reading this week's calendar"
  );
  assert.equal(
    activityLine({ kind: "task", status: "running", text: "Find this week's workout post" }),
    "Finding this week's workout post"
  );
  assert.equal(
    activityLine({ kind: "task", status: "awaiting_approval", text: "Post the summary" }),
    "Waiting for your approval"
  );
  // An unknown verb is left verbatim rather than invented.
  assert.equal(
    activityLine({ kind: "task", status: "running", text: "Audit the logs" }),
    "Audit the logs"
  );
  // No text at all → a neutral, truthful fallback.
  assert.equal(activityLine({ kind: "task", status: "running" }), "Working…");
}

// ── 5. truncation is safe and single-line ──────────────────────────
{
  const long = "Summarize every single conversation about the quarterly planning offsite";
  const line = activityLine({ kind: "task", status: "running", text: long });
  assert.ok(line.length <= 40, `len ${line.length} must be <= 40`);
  assert.ok(line.endsWith("…"), "truncated line ends with an ellipsis");
  assert.ok(!/[\n\r]/.test(line), "never multi-line");

  assert.equal(truncateLine("short line", 40), "short line");
  const wrapped = truncateLine("a\nb\nc", 40);
  assert.equal(wrapped, "a b c", "newlines collapse to spaces");
  assert.ok(truncateLine("x".repeat(100), 10).length <= 10);
}

// ── 6. buildActivityLines: durable descriptor + live precedence ────
{
  const bots = [{ id: "calendar-reader", name: "Calendar Reader" }];
  const conversations = [
    { conversation_id: "c1", bot_id: "calendar-reader", activity: { kind: "task", status: "running", text: "Read the calendar" } },
    { conversation_id: "c2", bot_id: "calendar-reader", activity: { kind: "delegation", status: "queued", text: "calendar request" } },
    { conversation_id: "c3", bot_id: "calendar-reader", activity: null },
    { conversation_id: "c4", activity: { kind: "task", status: "done", text: "Read the calendar" } },
  ];

  const base = buildActivityLines(conversations, { bots });
  assert.equal(base.c1, "Reading the calendar");
  assert.equal(base.c2, "Delegating calendar request");
  assert.equal(base.c3, undefined, "no work → no line");
  assert.equal(base.c4, undefined, "terminal work → no line");

  // A live overlay for the same conversation wins…
  const live = buildActivityLines(conversations, {
    bots,
    live: { c1: { kind: "delegation", status: "running", target_bot_id: "calendar-reader" } },
  });
  assert.equal(live.c1, "Waiting for Calendar Reader");

  // …and a NULL live entry clears a (stale) durable line.
  const cleared = buildActivityLines(conversations, { bots, live: { c1: null } });
  assert.equal(cleared.c1, undefined);
}

// ── 7. active subscriptions follow only live, identified tasks ─────
{
  const subs = activeSubscriptions([
    { conversation_id: "c1", activity: { kind: "task", status: "running", task_id: "t1" } },
    { conversation_id: "c2", activity: { kind: "task", status: "done", task_id: "t2" } },
    { conversation_id: "c3", activity: { kind: "delegation", status: "queued", task_id: "t3" } },
    { conversation_id: "c4", activity: { kind: "task", status: "running" } },
    { conversation_id: "c5", activity: null },
  ]);
  assert.deepEqual(
    subs.map((s) => `${s.conversationId}:${s.taskId}`),
    ["c1:t1", "c3:t3"]
  );
  assert.deepEqual(activeSubscriptions(undefined), []);
}

// ── 8. helper labels ───────────────────────────────────────────────
{
  assert.equal(humanizeBotId("calendar-reader"), "Calendar Reader");
  assert.equal(humanizeBotId(""), "");
  assert.equal(botDisplayName([{ id: "b1", name: "Chief of Staff" }], "b1"), "Chief of Staff");
  assert.equal(botDisplayName([], "b1"), "B1");
  assert.equal(botDisplayName([], ""), "");
}

console.log("✓ sidebar active-work line: durable-only derivation verified.");
