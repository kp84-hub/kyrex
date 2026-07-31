import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

export interface EngineMessage {
  type: string;
  content?: string;
  reasoning?: string;
  model?: string;
  provider?: string;
  context?: string;
  value?: string;
  name?: string;
  args?: unknown;
  result?: unknown;
  files?: { dirs: string[]; files: string[] };
  [key: string]: unknown;
}

type MessageHandler = (msg: EngineMessage) => void;
type ErrorHandler = (err: string) => void;

let unlistenMessage: UnlistenFn | null = null;
let unlistenError: UnlistenFn | null = null;
let unlistenStderr: UnlistenFn | null = null;
let unlistenClosed: UnlistenFn | null = null;

/**
 * Starts the bundled kyrex-engine sidecar binary (resolved by Tauri via
 * tauri.conf.json's externalBin entry, no path needed) and begins
 * listening for NDJSON messages relayed from Rust as "bridge-message" events.
 */
export async function startEngine(
  workspacePath: string,
  onMessage: MessageHandler,
  onError?: ErrorHandler,
  onStderr?: ErrorHandler,
  onClosed?: () => void
): Promise<void> {
  await stopListening();

  unlistenMessage = await listen<EngineMessage>("bridge-message", (event) => {
    onMessage(event.payload);
  });

  unlistenError = await listen<string>("bridge-error", (event) => {
    const raw = event.payload;
    // Bridge error format: "failed to parse line: {json_err} | raw: {line}"
    // If the raw line starts with [!] it's a readable engine message, surface it cleanly
    const marker = " | raw: ";
    const idx = raw.indexOf(marker);
    if (idx !== -1) {
      const rawLine = raw.slice(idx + marker.length);
      if (rawLine.startsWith("[!]") || rawLine.startsWith("[*]")) {
        onMessage({ type: "system", content: rawLine });
        return;
      }
    }
    onError?.(raw);
  });

  unlistenStderr = await listen<string>("bridge-stderr", (event) => {
    onStderr?.(event.payload);
  });

  unlistenClosed = await listen("bridge-closed", () => {
    onClosed?.();
  });

  await invoke("start_engine", { workspacePath });
}

export async function sendToEngine(payload: Record<string, unknown>): Promise<void> {
  await invoke("send_to_bridge", { payload: JSON.stringify(payload) });
}

export async function stopEngine(): Promise<void> {
  await invoke("stop_engine");
  await stopListening();
}

async function stopListening(): Promise<void> {
  unlistenMessage?.();
  unlistenError?.();
  unlistenStderr?.();
  unlistenClosed?.();
  unlistenMessage = null;
  unlistenError = null;
  unlistenStderr = null;
  unlistenClosed = null;
}

// ═══════════════════════════════════════════════════════════════════
// Race Mode
// ═══════════════════════════════════════════════════════════════════

export interface RaceLaneInfo {
  id: number;
  model: string;
  dir: string;
}

export interface RaceLaneEvent {
  laneId: number;
  event: EngineMessage;
}

type RaceLaneMessageHandler = (msg: RaceLaneEvent) => void;
type RaceLaneErrorHandler = (laneId: number, error: string) => void;
type RaceLaneClosedHandler = (laneId: number) => void;

let unlistenRaceMessage: UnlistenFn | null = null;
let unlistenRaceError: UnlistenFn | null = null;
let unlistenRaceClosed: UnlistenFn | null = null;

/**
 * Starts a race: N parallel engine lanes, one per model, each in its
 * own cloned workspace. Mirrors the TUI's race mode exactly (same
 * clone excludes, same auto-approve-on-confirm_request behavior).
 * Returns lane metadata (id, model, dir) once all lanes have spawned.
 */
export async function startRace(
  task: string,
  models: string[],
  workspacePath: string,
  onLaneMessage: RaceLaneMessageHandler,
  onLaneError?: RaceLaneErrorHandler,
  onLaneClosed?: RaceLaneClosedHandler
): Promise<RaceLaneInfo[]> {
  await stopRaceListening();

  unlistenRaceMessage = await listen<RaceLaneEvent>("race-lane-message", (event) => {
    onLaneMessage(event.payload);
  });
  unlistenRaceError = await listen<{ laneId: number; error: string }>(
    "race-lane-error",
    (event) => {
      onLaneError?.(event.payload.laneId, event.payload.error);
    }
  );
  unlistenRaceClosed = await listen<{ laneId: number }>("race-lane-closed", (event) => {
    onLaneClosed?.(event.payload.laneId);
  });

  return await invoke<RaceLaneInfo[]>("start_race", { task, models, workspacePath });
}

export async function stopRaceListening(): Promise<void> {
  unlistenRaceMessage?.();
  unlistenRaceError?.();
  unlistenRaceClosed?.();
  unlistenRaceMessage = null;
  unlistenRaceError = null;
  unlistenRaceClosed = null;
}

/// Computes a unified diff between the original workspace and a race
/// lane\'s clone directory. Returns the raw diff text (empty if no changes).
export async function diff_race_lane(
  workspacePath: string,
  laneDir: string
): Promise<string> {
  return await invoke<string>("diff_race_lane", { workspacePath, laneDir });
}

/// Merges a race lane\'s changes back into the real workspace by copying
/// modified/added files and deleting files that were removed in the lane.
/// Returns a summary of files changed. Must be called after the lane\'s
/// process has finished.
export async function mergeRaceLane(
  workspacePath: string,
  laneDir: string
): Promise<{ files_changed: number }> {
  return await invoke<{ files_changed: number }>("merge_race_lane", {
    workspacePath,
    laneDir,
  });
}

/// Kills all running lane child processes and clears RaceState. Call this
/// after merge or discard to return the UI to normal chat view.
export async function killRace(): Promise<void> {
  await invoke("kill_race");
  await stopRaceListening();
}
