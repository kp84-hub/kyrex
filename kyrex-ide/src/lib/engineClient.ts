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
    onError?.(event.payload);
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
