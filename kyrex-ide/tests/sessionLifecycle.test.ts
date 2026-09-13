/**
 * Focused tests for src/lib/sessionLifecycle.ts.
 *
 * Run:
 *   node --experimental-strip-types tests/sessionLifecycle.test.ts
 *
 * Self-contained PASS/FAIL harness (no node:test) — mirrors the repo's script
 * test convention and needs no dev dependencies. Kept outside src/ so `tsc`
 * (include: ["src"]) does not type-check it.
 */
import {
  afterClose,
  afterDisconnect,
  afterExplicitShutdown,
  afterTerminate,
  classifyReopen,
  describeStatus,
  isStale,
  DEFAULT_STALE_TTL_MS,
  type SessionIdentity,
} from "../src/lib/sessionLifecycle.ts";

let failures = 0;

function check(name: string, cond: boolean, extra = ""): void {
  if (cond) {
    console.log(`PASS ${name}`);
  } else {
    failures += 1;
    console.log(`FAIL ${name}${extra ? `: ${extra}` : ""}`);
  }
}

function eq<T>(name: string, got: T, want: T): void {
  check(name, got === want, `got ${String(got)} want ${String(want)}`);
}

const NOW = 1_700_000_000_000;

function identity(over: Partial<SessionIdentity> = {}): SessionIdentity {
  return {
    sessionId: "sess-abc123",
    pid: 4242,
    workspacePath: "/home/me/proj",
    startedAt: NOW - 1000,
    status: "active",
    ...over,
  };
}

// ── graceful IDE close ────────────────────────────────────────────────
eq("graceful close ends the session", afterClose(), "session-ended");
eq("closed label", describeStatus(afterClose()), "Session ended");

// ── active session state ──────────────────────────────────────────────
eq("active label", describeStatus("active"), "Engine ready");
eq("disconnected transition", afterDisconnect(), "disconnected");
eq("disconnected label", describeStatus("disconnected"), "Engine disconnected");

// ── reconnect to a live session ───────────────────────────────────────
{
  const cls = classifyReopen(identity(), { pidAlive: true, now: NOW });
  eq("live session -> reconnecting", cls.status, "reconnecting");
  eq("live session keeps id", cls.keepSessionId, true);
  eq("live session not stale", cls.stale, false);
  check(
    "reconnect admits the transport gap (not faked)",
    /not implemented|no live transport/i.test(cls.message),
    cls.message
  );
}

// ── engine already exited ─────────────────────────────────────────────
{
  const ended = classifyReopen(identity({ status: "ended" }), {
    pidAlive: false,
    now: NOW,
  });
  eq("ended identity -> session-ended", ended.status, "session-ended");
  eq("ended identity drops id", ended.keepSessionId, false);

  const dead = classifyReopen(identity(), { pidAlive: false, now: NOW });
  eq("dead pid -> session-ended", dead.status, "session-ended");
  eq("dead pid drops id", dead.keepSessionId, false);
  check("dead pid message", /no longer running/i.test(dead.message), dead.message);
}

// ── no recorded identity ──────────────────────────────────────────────
{
  const cls = classifyReopen(null, { pidAlive: false, now: NOW });
  eq("null identity -> session-ended", cls.status, "session-ended");
  eq("null identity keeps nothing", cls.keepSessionId, false);
}

// ── stale session identity ────────────────────────────────────────────
{
  const old = identity({ startedAt: NOW - (DEFAULT_STALE_TTL_MS + 1) });
  eq("old identity is stale", isStale(old, NOW), true);
  const cls = classifyReopen(old, { pidAlive: true, now: NOW });
  eq("stale is flagged", cls.stale, true);
  eq("staleness does not fabricate liveness", cls.status, "reconnecting");

  const fresh = identity({ startedAt: NOW - 1000 });
  eq("fresh identity is not stale", isStale(fresh, NOW), false);
}

// ── explicit shutdown / no orphan assumption ──────────────────────────
{
  const { status, identity: id } = afterExplicitShutdown();
  eq("explicit shutdown -> session-ended", status, "session-ended");
  eq("explicit shutdown clears identity", id, null);
  eq("terminate -> session-ended", afterTerminate(), "session-ended");
}

console.log(`\n${failures === 0 ? "ALL TESTS PASSED" : `${failures} FAILURE(S)`}`);
if (failures > 0) {
  throw new Error(`${failures} test(s) failed`);
}
