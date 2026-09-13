/**
 * Session lifecycle transitions — the pure logic behind the IDE's engine
 * session status. Dependency-free (plain erasable TypeScript) so Node can
 * verify it directly (`node --experimental-strip-types`), mirroring
 * chatProtocol.ts / engineFailure.ts.
 *
 * Honesty contract: the engine is supervised by THIS window's process
 * (EngineState.child), not by an independent daemon. When the window closes we
 * perform an explicit graceful shutdown and the session ends. A reopen can see
 * a *persisted identity* but has no live transport to it; this module therefore
 * never claims a session was silently resumed. `reconnecting` means exactly
 * "identity found, process still alive, transport reattach not yet available".
 */

export type SessionStatus =
  | "active"
  | "disconnected"
  | "reconnecting"
  | "session-ended";

/** Persisted status written by the Rust shell. */
export type PersistedStatus = "active" | "ended";

export interface SessionIdentity {
  sessionId: string;
  pid: number;
  workspacePath: string;
  /** epoch milliseconds */
  startedAt: number;
  status: PersistedStatus;
}

export interface ReopenClassification {
  status: SessionStatus;
  message: string;
  /** True when the note refers to a session that may still be reachable. */
  keepSessionId: boolean;
  /** True when the identity record is older than the staleness window. */
  stale: boolean;
}

/** An identity record older than this is reported as stale. */
export const DEFAULT_STALE_TTL_MS = 12 * 60 * 60 * 1000;

export function isStale(
  identity: SessionIdentity,
  now: number,
  ttlMs: number = DEFAULT_STALE_TTL_MS
): boolean {
  return now - identity.startedAt > ttlMs;
}

/**
 * Classify what a freshly-opened window should show about a prior session.
 *
 * - No identity            → session-ended (nothing recorded).
 * - Identity marked ended  → session-ended.
 * - Process not alive       → session-ended (engine already exited).
 * - Process still alive     → reconnecting: keep the id, but say plainly that
 *                             transport reattach is not implemented, so the
 *                             session is NOT silently adopted.
 */
export function classifyReopen(
  identity: SessionIdentity | null,
  opts: { pidAlive: boolean; now: number; staleTtlMs?: number }
): ReopenClassification {
  if (!identity) {
    return {
      status: "session-ended",
      message: "No engine session was recorded.",
      keepSessionId: false,
      stale: false,
    };
  }
  const stale = isStale(identity, opts.now, opts.staleTtlMs);
  if (identity.status === "ended") {
    return {
      status: "session-ended",
      message: "The previous engine session has ended.",
      keepSessionId: false,
      stale,
    };
  }
  if (!opts.pidAlive) {
    return {
      status: "session-ended",
      message: `Engine process (pid ${identity.pid}) is no longer running.`,
      keepSessionId: false,
      stale,
    };
  }
  return {
    status: "reconnecting",
    message:
      `A previous engine session is still running (pid ${identity.pid}), but ` +
      `this window has no live transport to it. Reconnecting requires the ` +
      `engine socket, which is not implemented yet — the session is not ` +
      `silently resumed.`,
    keepSessionId: true,
    stale,
  };
}

/** Explicit window close: the session ends (we shut the engine down). */
export function afterClose(): SessionStatus {
  return "session-ended";
}

/** The engine process reported termination (bridge-closed). */
export function afterTerminate(): SessionStatus {
  return "session-ended";
}

/** Bridge transport lost while the process state is unknown. */
export function afterDisconnect(): SessionStatus {
  return "disconnected";
}

/**
 * Explicit graceful shutdown: the child is killed AND the persisted identity
 * is dropped, so no stale "live session" record and no orphan process survive.
 */
export function afterExplicitShutdown(): {
  status: SessionStatus;
  identity: null;
} {
  return { status: "session-ended", identity: null };
}

export function describeStatus(status: SessionStatus): string {
  switch (status) {
    case "active":
      return "Engine ready";
    case "disconnected":
      return "Engine disconnected";
    case "reconnecting":
      return "Reconnecting to session…";
    case "session-ended":
      return "Session ended";
  }
}
