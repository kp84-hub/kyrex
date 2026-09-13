<!-- Project started: 2026-07-30 -->

# Tauri + React + Typescript

This template should help get you started developing with Tauri, React and Typescript in Vite.

## Recommended IDE Setup

- [VS Code](https://code.visualstudio.com/) + [Tauri](https://marketplace.visualstudio.com/items?itemName=tauri-apps.tauri-vscode) + [rust-analyzer](https://marketplace.visualstudio.com/items?itemName=rust-lang.rust-analyzer)

## Background engine (daemon mode)

The engine does not die with the app. On startup the IDE prefers to spawn or
reattach to a **detached engine daemon** (`KYREX_DAEMON=1`) that hosts a
localhost TCP socket:

- **Closing the app leaves the session running.** An in-flight turn finishes
  in the background; history keeps being saved to `.px_sessions/`.
- **Edit gates are auto-approved while no UI is attached** (matching race-mode
  precedent); **deletion gates are auto-denied** — nothing destructive runs
  unattended.
- **Reopening the app reattaches** to the live engine and replays everything
  it emitted while the app was closed (`session_replay`), so the transcript
  picks up exactly where it left off.
- The daemon exits by itself after 15 minutes of idleness with no UI attached
  (tunable via `KYREX_DAEMON_IDLE_EXIT`), and always saves the session first.

Discovery works via a control file (`~/.kyrex/daemons/{workspace-key}.json`)
holding `{pid, port}`; the workspace key is FNV-1a 64 of the normalized
workspace path and must match between `kyrex_engine/daemon_bridge.py` and
`src-tauri/src/daemon.rs`. If the engine binary is missing or daemon spawning
fails, the IDE falls back to the classic in-app child process (which dies with
the app, as before).

Built with Kyrex