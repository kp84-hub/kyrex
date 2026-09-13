# Kyrex Engine

## Daemon mode (background engine)

`core_bridge.py` can run as a detached daemon that outlives the UI that
spawned it (used by Kyrex IDE so closing the app doesn't kill the session):

- Spawn with `KYREX_DAEMON=1` (and `WORKSPACE_ROOT` set). The engine hosts a
  localhost TCP socket and records `{pid, port}` in
  `~/.kyrex/daemons/{workspace-key}.json`.
- Every stdout line is buffered (ring of 800) so a UI that attaches later
  receives a `session_replay` marker plus everything it missed; replayed
  approval requests are tagged `"replay": true` so UIs don't reopen stale
  modals.
- While no UI is attached, edit gates (`propose_edit`, `confirm_request`
  with `value: "edit"`) are auto-approved — matching race-mode precedent —
  and deletion gates are auto-DENIED. Nothing destructive runs unattended.
- With no client and no active turn, the daemon saves the session and exits
  after `KYREX_DAEMON_IDLE_EXIT` seconds (default 900; `0` disables).
- Clients send NDJSON lines on the socket; `{"type": "shutdown"}` stops the
  daemon gracefully (session saved, control file removed).

See `daemon_bridge.py` and `test_daemon_bridge.py`; run the integration check
with `python3 daemon_smoke_test.py` (needs engine deps installed).
