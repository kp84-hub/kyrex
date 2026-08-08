# Kyrex TUI — Upgrade Audit

> Generated 2025-07-17 after reviewing the full TUI codebase:
> `model.go`, `view.go`, `update.go`, `update_keys.go`, `update_engine.go`,
> `styles.go`, `renderer.go`, `metrics.go`, `race_overview.go`, `consult.go`,
> `race.go`, `components/diff.go`, `components/execution_timeline.go`, `view_backup.go`

---

## 🔴 HIGH Impact / Low Risk

### 1. Sidebar extraction into a proper component

**Problem:** `View()` inlines ~150 lines of sidebar rendering (CONTEXT section with tokens/cost, Active Files, Workspace tree, Execution Timeline with dynamic height math against remaining sidebar space, truncation logic). The `SidebarModel` struct exists but is a data container only — it has no `View()` method.

**Fix:** Extract into `Sidebar.View(width, height, usageStats, ...) string`. This:
- Slims `View()` by ~150 lines
- Enables standalone testing of sidebar layout, truncation, and timeline height calculation
- Makes the cache-key pattern (`_cachedSidebarKey`) a private implementation detail of the component

---

### 2. View() decomposition

**Problem:** `View()` is ~310 lines with 5 distinct render paths bolted together by `if/else` chains:
- Full-screen splash (`!m.HasSentFirstMessage`)
- Drag mode (`!m.MouseEnabled`)
- Normal mouse mode (sidebar + viewport + textarea + footer)
- Confirmation overlay (conditionally replaces main content)
- Race/Consult mode (separate layouts)

Each path has its own margin/padding/border math that's hard to test in isolation.

**Fix:** Extract named methods:
- `RenderConfirmOverlay(mainWidth int) string`
- `RenderNormalLayout(layout Layout) string`
- `RenderDragLayout() string`

`View()` becomes a ~30-line switch statement. Each render path becomes independently testable.

---

### 3. Model struct decomposition

**Problem:** The `Model` struct has 200+ fields. Seven distinct state clusters are all prefixed with `_` to indicate "ephemeral/internal" but share the top-level namespace:

| Cluster | Fields | Lines in struct |
|---------|--------|----------------|
| Race | `_raceComparing`, `_raceHighlight`, `_raceViewingDiff`, `_raceDiffs`, `_raceDiffLines`, `_raceDiffScroll`, `_raceMergePending`, `_raceNoOverview`, `_raceGates`, `_raceGateOutput`, `_raceGatesRunning`, `_raceViewingGate`, `_raceGateScroll`, `_raceWizardStep`, `_raceWizardTask`, `_raceModelPickerActive`, `_raceModelPicker*`, `_raceTaskSent` | ~18 fields |
| Consult | `_consultActive`, `_consult`, `_consultTaskSent`, `_consultWizardStep`, `_consultWizardTask`, `_consultModelPicker*`, `_consultConfirmPending`, `_consultConfirmModels`, `_consultConfirmFocus` | ~10 fields |
| Setup Flow | `_setupActive`, `_setupStep`, `_setupOllama`, `_setupProvider`, `_setupBaseURL`, `_setupAPIKey`, `_setupAPIKeyEnv`, `_setupModel`, `_setupModels`, `_setupFilteredModels`, `_setupModelFilter`, `_setupCustomModel`, `_setupHeaders`, `_setupTestResult`, `_setupTestPassed`, `_setupSaving`, `_setupError`, `_setupInput`, `_setupCursorPos` | ~18 fields |
| Model Picker | `_modelPickerActive`, `_modelPickerLoading`, `_modelPickerAllItems`, `_modelPickerItems`, `_modelPickerCurrent`, `_modelPickerFilter`, `_modelPickerInput`, `_modelPickerIndex` | 8 fields |
| Command Picker | `_cmdPickerActive`, `_cmdPickerItems`, `_cmdPickerIndex`, `_cmdPickerInput` | 4 fields |
| Layout Caches | `_lastAppliedVpWidth`, `_lastAppliedVpHeight`, `_lastAppliedTaWidth`, `_lastAppliedTaHeight`, `_lastAppliedShowSidebar`, `_lastAppliedLayout`, `_pendingTaHeight`, `_taHeightDebounce` | 8 fields |
| Token/Render Caches | `_cachedViewportContent`, `_cachedWidth`, `_cachedHistoryContent`, `_cachedHistoryLines`, `_cachedHistoryWidth`, `_historyCacheValid`, `_stableHistoryContent`, `_stableHistoryLines`, `_stableHistoryLen`, `_stableHistoryWidth`, `_lastSetContent` | ~11 fields |

**Fix:** Extract into dedicated sub-structs:
```go
type Model struct {
    // ... core fields remain (Phase, History, Turns, Viewport, Textarea, etc.)

    Race    RaceState
    Consult ConsultState
    Setup   SetupState
    Picker  PickerState      // model picker
    Cmd     CommandPickerState
    Cache   RenderCacheState
}
```

Benefits:
- Each sub-struct can have its own `Reset()` method (replaces one-off field assignments in `resetTurnState()`)
- Impossible to accidentally shadow a field name
- Grouped logically for anyone reading the code
- Testing: each state machine can be tested independently

---

## 🟡 MEDIUM Impact

### 4. Duplicate Race/Consult lane infrastructure

**Problem:** Race (~45 fields + ~300 LOC in `update.go`) and Consult (~20 fields + ~150 LOC) share nearly identical lane management: clone → spawn child engines → dispatch task → collect output → compute diffs → run gates → merge/return. But they have separate struct fields, separate message types (`RaceSetupMsg` vs `ConsultSetupMsg`, `LaneMsg` routed by `m._consultActive` check), and separate handler paths.

The `update.go` switch statement has this pattern:
```go
case race.LaneMsg:
    if m._consultActive && m._consult != nil {
        return m.handleConsultLaneMsg(msg)
    }
    if !m.RaceMode || m.Race == nil { break }
    // ... handler duplicated
```

**Fix:** A single `MultiLane` abstraction that both Race and Consult consume:
```go
type MultiLane struct {
    Lanes       []*race.Lane
    Task        string
    RoundCap    int
    Mode        LaneMode // RaceMode | ConsultMode
    OnSettled   func(result MultiLaneResult)
    // ...
}
```

Eliminates the "fix it in Race but forget Consult" risk.

---

### 5. Dead code: `RenderSplashScreen()` and `view_backup.go`

**Problem:**
- `RenderSplashScreen()` in `view.go` is defined but **never called** anywhere in `View()`. The full-screen splash (`RenderFullScreenSplash()`) handles the `!m.HasSentFirstMessage` path.
- `view_backup.go` exists alongside `view.go` — likely a snapshot from a refactor that was never removed.

**Fix:** Delete both. Reduces confusion for future maintainers.

---

### 6. Wizard step magic numbers

**Problem:** Multiple state machines use bare integer constants for wizard steps:
```go
_raceWizardStep int    // 0 = inactive, 1 = awaiting task, 2 = awaiting models
_consultWizardStep int // 0 = inactive, 1 = awaiting focus, 2 = awaiting models
_setupStep int         // 0=provider, 1=api_key, 2=model, 3=test, 4=save
```

Adding or reordering steps requires auditing every numeric reference.

**Fix:** Typed constants like `Phase` already does:
```go
type ConsultWizardStage int
const (
    ConsultWizardInactive ConsultWizardStage = iota
    ConsultWizardTask
    ConsultWizardModels
)
```

---

## 🟢 LOWER Impact / Nice-to-have

### 7. Paste detection heuristic

**Problem:** Paste is detected as "≥40 runes in a single `KeyRunes` event or containing 2+ newlines". The visible textarea content is replaced with `[Pasted ~N lines]` and the real text is buffered in `_realInputBuffer`. This:
- Overwrites any text the user had already typed (they could lose work)
- Doesn't distinguish fast typing from actual paste on high-latency connections
- The 40-char threshold is arbitrary

**Fix:** Track *inter-key timing* — if gap between consecutive key events is <5ms, it's a paste. If the user types 40 chars at human speed (150ms+ gaps), it's not a paste. Also: show `[Pasted ~N lines — Ctrl+Z to undo]` instead of silently replacing.

---

### 8. Auto-approve delay hardcoded

**Problem:** `AutoApproveDelay: 5 * time.Second` is hardcoded in `NewModel()`. The `/autoapprove` command only toggles on/off. The splash metadata line shows `auto-approve: on` but not the delay.

**Fix:** Support `/autoapprove 3` to set delay in seconds. Persist the configured delay so it survives restarts. Show in splash as `auto-approve: on (3s)`.

---

### 9. Toast notification history

**Problem:** `m.Toast` is a single string with an expiration time. If a second toast fires before the first expires, it's silently overwritten. There's no way to see past toasts.

**Fix:** Keep a small ring buffer (8-16 entries) of recent toasts. Accessible via a keybinding like `g t` or automatically shown in the sidebar footer. Particularly useful for gate/diff results in race mode that vanish when the next toast arrives.

---

### 10. `cellBuffer` renderer — dead code

**Problem:** `renderer.go` contains a `cellBuffer` struct that tracks what's rendered on screen and emits only ANSI escape codes for changed cells. It's **never instantiated or used** anywhere in the TUI. The application relies entirely on lipgloss for full-terminal rewrites.

**Fix:** Either:
- **Remove it** (simplest — dead code adds confusion)
- **Integrate it** as an optional lower-latency render mode for fast terminals (ANSI diffs are smaller than full rewrites, could reduce perceived latency during token streaming)

---

### 11. No session persistence / resume

**Problem:** When the TUI exits, all conversation state is lost. The engine-side session format (`.px_sessions/main.json`) already exists but the TUI doesn't load it on startup. No `/resume` command.

**Fix:** On startup, load the most recent session file from the current workspace's `.px_sessions/` directory. Rebuild `m.History` and `m.Turns` from the saved messages. Wire `/resume <session-id>` to load a specific session. This lets users pick up where they left off after an accidental close or terminal disconnect.

---

### 12. Side-by-side diff renderer is standalone

**Problem:** `renderSideBySide()` in `view.go` is a standalone function (not a method) with regex-based hunk header parsing and manual line-number tracking. It's tested only through `render_side_by_side_test.go`.

**Fix:** Make it a method on a `DiffRenderer` struct that holds column width, line numbering style, and color preferences. This would:
- Make it consistent with the `components.DiffBlock` type
- Allow testing color/gutter configurations independently
- Support future features like collapsed unchanged regions

---

## Summary Table

| # | Area | Impact | Effort | Quick Win? |
|---|------|--------|--------|-----------|
| 1 | Sidebar component | 🔴 High | 2 days | ✅ |
| 2 | View() decomposition | 🔴 High | 1 day | ✅ |
| 3 | Model struct decomposition | 🔴 High | 3 days | |
| 4 | Race/Consult dedup | 🟡 Med | 3-4 days | |
| 5 | Dead code removal | 🟡 Med | 1 hour | ✅ |
| 6 | Wizard typed constants | 🟡 Med | 3 hours | ✅ |
| 7 | Paste detection | 🟢 Low | 1 day | |
| 8 | Auto-approve delay config | 🟢 Low | 4 hours | ✅ |
| 9 | Toast history | 🟢 Low | 1 day | |
| 10 | cellBuffer cleanup | 🟢 Low | 2 hours | ✅ |
| 11 | Session persistence | 🟢 Low | 3-4 days | |
| 12 | DiffRenderer component | 🟢 Low | 1 day | |