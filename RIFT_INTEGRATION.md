# RIFT_INTEGRATION.md

Instructions for integrating the `rift` copy-on-write workspace package into
Kyrex. **You (the coding agent) are the reader.** Work one phase at a time.
Stop at each phase boundary, run the stated checks, print the real output, and
show `git diff --stat`. Do not report a phase complete unless the build passes
AND `git diff --stat` shows the expected files. If a check fails, stop and
report — do not proceed.

The goal: run each agent session inside an isolated clone of the project so the
live tree is never touched until the user approves. The diff shown to the user
is then computed from disk, not self-reported.

---

## Phase 0 — Place the package and confirm it builds

The `rift` package is pure standard library (no third-party imports). It must
live under this repo's module.

1. Confirm the module path: `head -1 go.mod`. Call it `MODULE`.
2. Place the seven package files at `internal/rift/`:
   `backend.go copy.go git.go manager.go reflink_linux.go reflink_darwin.go reflink_stub.go`
   (Source: the downloaded `kyrex-rift/rift/` directory. Copy only that
   directory's `.go` files. Do NOT copy the bundled `go.mod` — this repo's
   `go.mod` governs. The `cmd/kyrex-rift` CLI is optional and not needed for the
   integration; skip it unless explicitly asked.)
3. The package's import path is `MODULE/internal/rift`.

**Checks:** `go build ./internal/rift/` and `go vet ./internal/rift/` both clean.
Then `git diff --stat` (should show only new files under `internal/rift/`).

---

## Phase 1 — Discovery (READ ONLY — make no edits)

Locate and report, as `file:line`, each of the following. Print the relevant
lines. Edit nothing.

1. Where the Python engine subprocess is created in the Go TUI
   (`exec.Command` / `exec.CommandContext` for the engine binary).
2. Where `WORKSPACE_ROOT` is set in the engine's environment (`cmd.Env`).
3. The variable holding the project/repo root path that is passed to the engine
   as `WORKSPACE_ROOT`. Call it `projectRoot`.
4. The struct that owns the engine session / Bubble Tea model (where per-session
   state lives). Call it `Session`.
5. Where a `task_complete` signal ends the agent loop, and where the existing
   edit-approval / diff gate is rendered and resolved (approve vs. reject).

Report all five before doing anything in Phase 2.

---

## Phase 2 — Create a workspace around the engine launch

Using the locations from Phase 1:

1. Import `MODULE/internal/rift`.
2. Add a field to `Session`: `workspace *rift.Workspace`.
3. Immediately before the engine subprocess is spawned, create a workspace and
   repoint the engine at it:

   ```go
   mgr := rift.New()
   ws, err := mgr.Create(projectRoot, runLabel) // runLabel: any short session id, or ""
   if err != nil {
       // fall back to running against the live tree; do not abort the run
       log.Printf("rift: clone failed, using live project: %v", err)
       ws = &rift.Workspace{Root: projectRoot, Source: projectRoot}
   }
   session.workspace = ws
   ```

4. Set the engine env to the clone: `WORKSPACE_ROOT=ws.Root` (replace the
   existing `projectRoot` value at the `cmd.Env` site found in Phase 1).

Keep a single `rift.Manager` (e.g. a package-level or Session-level value) so
later phases can call `MergeBack` / `Discard` on the same manager.

**Checks:** `go build ./...` clean. Manually start a session, have the agent
edit one file, confirm the file under `projectRoot` is unchanged while the edit
appears under `session.workspace.Root`. Show `git diff --stat`.

---

## Phase 3 — Approve / discard at the gate

Wire the workspace lifecycle into the existing approval gate (Phase 1, item 5):

1. On **approve**: `changes, err := mgr.MergeBack(session.workspace)` — this
   copies the agent's changes onto `projectRoot`. Surface `len(changes)` /
   errors in the UI. After a successful merge, discard the clone:
   `mgr.Discard(session.workspace)`.
2. On **reject / cancel**: `mgr.Discard(session.workspace)` — live tree stays
   untouched.
3. On **session end or engine crash without approval**: `mgr.Discard(...)` as
   cleanup so clones don't accumulate. Skip discard if `workspace.Root ==
   workspace.Source` (the Phase 2 fallback case).
4. The diff shown in the gate must reflect the clone. Either:
   - switch the existing diff source to run against `session.workspace.Root`, or
   - use `mgr.Changes(session.workspace)` (returns `[]rift.Change{Path, Kind}`)
     and `mgr.Diff(session.workspace)` (unified diff of tracked edits).

**Checks:** `go build ./...` clean. Run two sessions: approve one (verify the
edit lands in `projectRoot` via `git diff --stat`), reject the other (verify
`projectRoot` is clean and the clone directory is gone).

---

## Phase 4 — Verify the isolation assumption (CRITICAL)

The whole approach assumes every file write the engine performs resolves under
`WORKSPACE_ROOT`. Verify this; if any tool bypasses it, edits leak to the live
tree.

1. In the Python engine, grep the file tools (read/write/edit/create/delete,
   shell tool, anything that touches the filesystem) for how they resolve paths.
2. Confirm each resolves relative to `WORKSPACE_ROOT` (or the engine's CWD set
   to `WORKSPACE_ROOT`), with no absolute project paths and no repo root cached
   at import time.
3. If a tool bypasses `WORKSPACE_ROOT`, fix it to join paths under
   `WORKSPACE_ROOT`.

**Check:** exercise each write tool once and confirm the file appears under the
clone root, never under `projectRoot`.

---

## Phase 5 — Optional: surface the backend

`session.workspace.Backend` is `"reflink"` (CoW active) or `"copy"` (full copy,
e.g. ext4/WSL2). Show it once per session in the status line or a log line so
it's visible whether CoW is in effect. On WSL2/ext4 it will read `copy`; that is
expected and correct — moving the project onto a btrfs mount flips it to
`reflink` with no code change.

---

## Guardrails (apply throughout)

- Make the minimum edits required; `gofmt -w` touched files.
- Never mark a phase done on a self-report — prove it with `go build`, `go vet`,
  and `git diff --stat` output.
- Do not commit. Leave changes staged/unstaged for the user to review.
- Stop at every phase boundary for human review.
