package tui

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/kp84-hub/kx/tui/components"

	"github.com/kp84-hub/kx/internal/rift"
)

// History entry prefixes for the scripted conversation transcript. Completed
// provider rounds are committed as discrete (KYREX, Thought) pairs so the
// transcript reads KYREX → Thought → tool → … instead of one accumulated dump.
// The final Overview only ever carries the engine's own task_complete summary.
const (
	historyAssistant = "_Assistant:_"
	historyThought   = "_Thought:_"

	// roundSeparator is the cosmetic divider the engine streams into the token
	// stream between provider rounds of a single turn ("\n\n---\n" in
	// kyrex/core.py). The TUI treats it as a round boundary: the completed
	// round is committed to history and the divider itself is dropped.
	roundSeparator = "\n\n---\n"
)

// extractTaskCompleteSummary pulls the model's own "[Task Complete: …]"
// summary out of the engine's final payload. Returns "" when the model did
// not signal completion, so the TUI never invents an Overview.
func extractTaskCompleteSummary(final string) string {
	idx := strings.LastIndex(final, "[Task Complete: ")
	if idx < 0 {
		return ""
	}
	rest := final[idx+len("[Task Complete: "):]
	rest = strings.TrimSuffix(rest, "]")
	return strings.TrimSpace(rest)
}

// surfaceThoughtAsBlock reports whether pending reasoning should be committed
// as a visible Thought at this boundary, per the conversational pacing policy.
// Reasoning accumulates internally and only surfaces as a Thought when it
// attaches to meaningful KYREX content, or when it is the first reasoning
// chain since the last meaningful activity (content / tool / phase change).
// Later bare chains coalesce in the live buffer instead of stacking another
// Thought block. Strictly a presentation policy: the reasoning text is never
// altered, only whether a boundary promotes it to a visible block.
func (m Model) surfaceThoughtAsBlock(content string, reasoning string) bool {
	if reasoning == "" {
		return false
	}
	// Meaningful KYREX content re-opens the latch — the coalesced reasoning
	// surfaces as this content's Thought (the Thought → KYREX orientation).
	if content != "" {
		return true
	}
	// Bare reasoning-only boundaries surface at most one leading Thought per
	// stretch of meaningful activity, so consecutive reasoning events can
	// never render as a run of Thought blocks.
	return !m._suppressThought
}

// commitPair appends a finished (KYREX content, reasoning) pair to history
// applying the Thought pacing policy: content always lands, reasoning only
// surfaces per surfaceThoughtAsBlock, and the suppression latch tracks
// whether a Thought was just surfaced. Live buffer clearing is the caller's
// job — commitRound keeps suppressed reasoning alive for coalescing.
func (m Model) commitPair(content string, reasoning string) Model {
	if content == "" && reasoning == "" {
		return m
	}
	if content != "" {
		m.History = append(m.History, historyAssistant+"\n"+content)
	}
	surfaced := m.surfaceThoughtAsBlock(content, reasoning)
	if surfaced {
		m.History = append(m.History, historyThought+"\n"+reasoning)
		m._suppressThought = true
	} else if content != "" {
		// Meaningful assistant content without a Thought reopens the latch.
		m._suppressThought = false
	}
	return m
}

// commitRound flushes the in-flight provider round (streamed KYREX content +
// reasoning) into history as a discrete Assistant + Thought pair and clears
// the live buffers. It is a presentation-layer segmentation of the engine's
// own event stream: each round's content and reasoning are committed when the
// round ends (stream separator / tool_start / chat_done), so the transcript
// reads as KYREX → Thought → tool → … rather than one giant block. No content
// is fabricated and no hidden reasoning is exposed — only what the engine
// already streamed to the TUI. Bare reasoning-only boundaries are coalesced
// (kept live, not committed) when the Thought latch is closed, so a burst of
// consecutive reasoning rounds never becomes a run of Thought blocks.
func (m Model) commitRound() Model {
	content := m.CurrToken
	reasoning := m.Reasoning
	if content == "" && reasoning == "" {
		return m
	}
	before := len(m.History)
	m = m.commitPair(content, reasoning)
	m.CurrToken = ""
	if len(m.History) == before {
		// The bare chain was coalesced, not surfaced: it stays in the live
		// buffer so the next chain merges into one eventual Thought. Nothing
		// changed in history, so the stable cache stays valid.
		return m
	}
	m.Reasoning = ""
	m._cachedViewportContent = ""
	m._stableHistoryContent = "" // invalidate stable cache — history changed
	m._viewportDirty = true
	return m
}

// handleEngineMsg processes messages from the Python engine.
// Returns (model, cmd, handled) where handled=true means the caller should return immediately.
func (m Model) handleEngineMsg(msg MsgFromEngine) (Model, tea.Cmd, bool) {
	// Drop stale engine messages after clear/reset (except session_state)
	if m._suppressEngine && msg.Type != "session_state" && msg.Type != "tui_pause" {
		return m, nil, true
	}

	switch msg.Type {
	case "tui_pause":
		return m.handlePause(msg)
	case "token", "content":
		m.IsSending = false
		m.IsThinking = false
		m._interruptPending = false
		// The engine streams "\n\n---\n" between provider rounds of a turn.
		// Treat it as a round boundary: commit the completed KYREX/Thought
		// pair and drop the cosmetic divider.
		if msg.Content == roundSeparator {
			m = m.commitRound()
			if !m._tokenCoalescePending {
				m._tokenCoalescePending = true
				return m, tokenCoalesceCmd(), false
			}
			return m, nil, false
		}
		m.CurrToken += msg.Content
		m._viewportDirty = true
		// Token coalescing: accumulate immediately, schedule one 16ms flush.
		// Multiple tokens arriving within the window batch into a single redraw.
		if !m._tokenCoalescePending {
			m._tokenCoalescePending = true
			return m, tokenCoalesceCmd(), false
		}
	case "log":
		m.History = append(m.History, "_Logs:_\n"+msg.Content)
		m._viewportDirty = true
	case "reasoning":
		m.IsSending = false
		m.IsThinking = true
		if msg.Content != "" {
			m.Reasoning += msg.Content
		} else if msg.Reasoning != "" {
			m.Reasoning += msg.Reasoning
		}
		m._viewportDirty = true
		// Token coalescing for reasoning stream (same 16ms batch window)
		if !m._tokenCoalescePending {
			m._tokenCoalescePending = true
			return m, tokenCoalesceCmd(), false
		}
	case "chat_done":
		return m.handleChatDone(msg)
	case "phase":
		return m.handlePhase(msg)
	case "tool_start":
		return m.handleToolStart(msg)
	case "tool_result":
		return m.handleToolResult(msg)
	case "confirm_request":
		return m.handleConfirmRequest(msg)
	case "diff":
		return m.handleDiff(msg)
	case "error":
		return m.handleError(msg)
	case "session_state":
		return m.handleSessionState(msg)
	}

	return m, nil, false
}

func (m Model) handlePause(msg MsgFromEngine) (Model, tea.Cmd, bool) {
	if msg.Value == "usage_stats_silent" {
		if statsMap, ok := msg.Files.(map[string]interface{}); ok {
			m._usageStats = statsMap
			// No overlay — silent sidebar update only
		}
		return m, nil, true
	}
	if msg.Value == "usage_stats" {
		if statsMap, ok := msg.Files.(map[string]interface{}); ok {
			m._usageStats = statsMap
			m._usageOverlayActive = true
		}
		return m, nil, true
	}
	if msg.Value == "model_picker" {
		m._modelPickerActive = true
		m._modelPickerItems = nil
		m._modelPickerCurrent = msg.Model
		m._modelPickerIndex = 0
		if filesList, ok := msg.Files.([]interface{}); ok {
			for _, item := range filesList {
				if s, ok := item.(string); ok {
					m._modelPickerItems = append(m._modelPickerItems, s)
				}
			}
		}
		// Set initial arrow position to current model if found
		for i, name := range m._modelPickerItems {
			if name == m._modelPickerCurrent {
				m._modelPickerIndex = i
				break
			}
		}
		m._modelPickerInput = ""
	}
	if msg.Value == "mcp_connection_result" {
		if raw, err := json.Marshal(msg.Files); err == nil {
			var result MCPConnectionResult
			if err := json.Unmarshal(raw, &result); err == nil {
				m._mcpTestResult = &result
				if result.Success {
					m.Toast = fmt.Sprintf("MCP connection succeeded: %d tool(s) discovered", result.ToolCount)
				} else {
					m.Toast = fmt.Sprintf("MCP connection failed: %s", result.Error)
				}
				m.ToastEnd = time.Now().Add(5 * time.Second)
			}
		}
		return m, nil, true
	}
	if msg.Value == "mcp_connector_picker" {
		m._mcpPickerActive = true
		m._mcpPickerAllItems = nil
		m._mcpPickerItems = nil
		m._mcpPickerCurrent = msg.Model
		m._mcpPickerFilter = ""
		m._mcpPickerInput = ""
		m._mcpPickerIndex = 0

		if raw, err := json.Marshal(msg.Files); err == nil {
			if err := json.Unmarshal(raw, &m._mcpPickerAllItems); err != nil {
				m.Toast = fmt.Sprintf("MCP connector data invalid: %v", err)
				m.ToastEnd = time.Now().Add(4 * time.Second)
			} else {
				sort.SliceStable(m._mcpPickerAllItems, func(i, j int) bool {
					if m._mcpPickerAllItems[i].Category != m._mcpPickerAllItems[j].Category {
						return m._mcpPickerAllItems[i].Category < m._mcpPickerAllItems[j].Category
					}
					return m._mcpPickerAllItems[i].Name < m._mcpPickerAllItems[j].Name
				})
				m._mcpPickerItems = append([]MCPConnector(nil), m._mcpPickerAllItems...)
			}
		} else {
			m.Toast = fmt.Sprintf("MCP connector data unavailable: %v", err)
			m.ToastEnd = time.Now().Add(4 * time.Second)
		}
	}
	return m, nil, true
}

// hasCommittedRoundsThisTurn reports whether round segments were already
// committed to history for the current user turn (i.e. content arrived for
// earlier rounds and was flushed at a round boundary). handleChatDone uses it
// to avoid re-committing the engine's whole-turn payload when the final round
// streamed no content of its own.
func (m Model) hasCommittedRoundsThisTurn() bool {
	for i := len(m.History) - 1; i >= 0; i-- {
		h := m.History[i]
		if strings.HasPrefix(h, "> ") {
			return false
		}
		if strings.HasPrefix(h, historyAssistant) {
			return true
		}
	}
	return false
}

func (m Model) handleChatDone(msg MsgFromEngine) (Model, tea.Cmd, bool) {
	// Cancel any pending coalesce tick — chat_done does an immediate flush
	m._tokenCoalescePending = false

	// Commit the final in-flight round as a discrete KYREX/Thought pair, the
	// same way earlier rounds were committed at their tool/stream boundaries.
	// msg.Content/msg.Reasoning are the WHOLE-turn concatenations, so they are
	// only a fallback when the final round streamed nothing — using them
	// unconditionally would duplicate every round already committed.
	finalStreamed := m.CurrToken
	if finalStreamed == "" && msg.Content != "" {
		// The engine substitutes this placeholder when a turn produced only
		// reasoning — it is not assistant speech, so never surface it.
		// Also skip the fallback when earlier rounds were already committed at
		// their boundaries: msg.Content is the whole-turn concatenation and
		// re-committing it would duplicate every round.
		if !strings.HasPrefix(msg.Content, "[Model produced reasoning but no display content") && !m.hasCommittedRoundsThisTurn() {
			finalStreamed = msg.Content
		}
	}
	finalReasoned := m.Reasoning
	if finalReasoned == "" && msg.Reasoning != "" {
		finalReasoned = msg.Reasoning
	}

	// Concise final Overview: only the model's own task_complete summary is
	// used. The TUI never invents an Overview from accumulated content.
	overview := extractTaskCompleteSummary(msg.Content)

	// If the fallback text carried the task_complete marker (it is appended
	// to the engine's payload, never streamed), strip it before display —
	// the summary already lives in its own Overview block below.
	if overview != "" {
		if idx := strings.LastIndex(finalStreamed, "\n[Task Complete: "); idx >= 0 {
			finalStreamed = finalStreamed[:idx]
		}
	}

	m.IsThinking = false

	// Commit the final pair under the same pacing policy as round boundaries:
	// content always lands; trailing reasoning surfaces as a Thought only
	// when it attaches to that content or no Thought has surfaced since the
	// last meaningful activity (keeps TestThoughtOnlyEvent's single-thought
	// contract while never stacking thoughts).
	m = m.commitPair(finalStreamed, finalReasoned)
	if overview != "" {
		m.History = append(m.History, "_Overview:_\n"+overview)
	}

	m._cachedViewportContent = ""
	m._stableHistoryContent = "" // invalidate stable cache — history just changed
	m._viewportDirty = true
	m.CurrToken = ""
	m.Reasoning = ""

	content := m.FullViewportContent(m.Viewport.Width)
	m.Viewport.SetContent(content)
	m._lastSetContent = content
	if !m.ScrollLock {
		m.Viewport.GotoBottom()
	}
	m._viewportDirty = false
	m._lastViewportFlush = time.Now()

	m.Timeline.Add(components.TimelineEvent{
		Type:      components.EventExecution,
		Status:    components.StatusSuccess,
		Title:     "Response complete",
		Timestamp: time.Now(),
	})
	m.MissionSummary = m.generateMissionSummary()

	// Clear diff/confirm state so the overview renders instead of stale panes.
	// An engine-backed Gate A confirmation must never be wiped silently: if
	// one is somehow still pending at turn completion, settle it as an
	// explicit denial so the engine-side operation parked on its approval wait
	// receives its decision instead of hanging for the full timeout.
	m.DiffBlocks = nil
	m.ActiveDiffID = ""
	if m.ConfirmID != "" {
		m = m.resolvePendingConfirmAsDenied()
	} else {
		m.ConfirmPath = ""
		m.ConfirmDiff = ""
		m.ConfirmType = ""
		m.ConfirmPaths = nil
	}

	// Only re-render if the sweep actually appended something. handleChatDone
	// already flushed the viewport above; repeating that on every turn rebuilds
	// the whole history for nothing.
	if m.detectUnmergedChanges() {
		content = m.FullViewportContent(m.Viewport.Width)
		m.Viewport.SetContent(content)
		m._lastSetContent = content
		m._cachedViewportContent = ""
		m._stableHistoryContent = ""
		if !m.ScrollLock {
			m.Viewport.GotoBottom()
		}
	}

	return m, nil, true
}

// detectUnmergedChanges reports anything sitting in the clone that the
// per-file approval gate did not merge, and returns true if it appended to
// History so the caller knows whether a re-render is needed.
//
// Only edit_file and write_file_with_gate route through the confirm gate.
// Anything run_command writes to disk produces no diff, never calls
// MergeFile, and is discarded with the clone. Asking git what changed is
// agnostic to which tool changed it.
//
// This reports even when auto-approve is on: auto-approve means "do not make
// me read diffs for edits I would have approved", and these never produced a
// diff at all.
func (m *Model) detectUnmergedChanges() bool {
	if m.Workspace == nil || m.WorkspaceMgr == nil || m.Workspace.Root == m.Workspace.Source {
		return false
	}

	changes, err := m.WorkspaceMgr.Changes(m.Workspace)
	if err != nil {
		// Changes() needs a git repo. Warn once per session rather than every
		// turn, but do not go quiet: in a non-git project, shell-written edits
		// vanish with the clone and the operator has no way to know.
		if m.SweepWarned {
			return false
		}
		m.SweepWarned = true
		m.History = append(m.History,
			"\u26a0  Cannot inspect clone changes (not a git repo). Edits made "+
				"outside the approval gate will be lost when the clone is discarded.")
		return true
	}
	// Reset the warning here, not in the empty-changes branch: a repo that
	// starts responding to git again should be able to warn once more if it
	// later stops.
	m.SweepWarned = false
	if len(changes) == 0 {
		m.SweepActive = false
		m.SweepChanges = nil
		m._sweepCardStart, m._sweepCardEnd = 0, 0
		return false
	}

	// Drop anything that was already dirty before the session started.
	fresh := changes[:0:0]
	for _, c := range changes {
		if !m.SweepBaseline[c.Path] {
			fresh = append(fresh, c)
		}
	}
	if len(fresh) == 0 {
		m.SweepActive = false
		m.SweepChanges = nil
		m._sweepCardStart, m._sweepCardEnd = 0, 0
		return false
	}
	changes = fresh

	m.SweepActive = true
	m.SweepChanges = changes
	// One live presentation: if a previous sweep card is still pending it is
	// replaced, never duplicated, so the viewport shows exactly one active
	// Gate B approval card at a time.
	m.presentSweepCard(changes)
	return true
}

// presentSweepCard maintains exactly one live "bypassed diff gate" approval
// card in History. If a previous card is still pending, only its own lines
// (the change list plus the y/n prompt) are removed and the fresh list is
// presented at the current end of the transcript. Everything the user or the
// engine streamed in between — the next turn's transcript, approval results —
// is untouched, so the audit record stays intact.
func (m *Model) presentSweepCard(changes []rift.Change) {
	if m._sweepCardStart < m._sweepCardEnd {
		if m._sweepCardStart >= 0 && m._sweepCardEnd <= len(m.History) {
			m.History = append(m.History[:m._sweepCardStart], m.History[m._sweepCardEnd:]...)
		}
		m._sweepCardStart, m._sweepCardEnd = 0, 0
	}
	start := len(m.History)
	for _, change := range changes {
		m.History = append(m.History,
			fmt.Sprintf("  %s  %s", change.Kind, change.Path))
	}
	m.History = append(m.History, fmt.Sprintf(
		"\u26a0  %d change(s) above bypassed the diff gate (run_command writes "+
			"to disk directly). Press y to merge into the project, n to discard.",
		len(changes)))
	m._sweepCardStart = start
	m._sweepCardEnd = len(m.History)
}

// resolvePendingConfirmAsDenied settles an engine-backed Gate A confirmation
// that is being dismissed programmatically (turn reset, /new, chat_done) with
// an explicit DENY: the engine-side operation parked on its approval wait
// receives its decision and terminates cleanly instead of hanging for the
// 300s timeout. It never auto-approves. When no gate is pending it is a no-op.
func (m Model) resolvePendingConfirmAsDenied() Model {
	if m.ConfirmID == "" {
		return m
	}
	if m.SendFunc != nil {
		_ = m.SendFunc(map[string]interface{}{
			"type":     "confirm_response",
			"id":       m.ConfirmID,
			"approved": false,
		})
	}
	// Audit: the proposal was dismissed without a user decision. Keep the
	// collapsed-line convention so repeated dismissals collapse, and distinct
	// text so they never conflate with explicit y/n approvals or rejections.
	m = m.appendCollapsedApprovalLine("↷  Pending change to: " + m.ConfirmPath + " — dismissed, not applied")
	m.Timeline.UpdateByID(m.ConfirmID, components.StatusWarning, "Dismissed (no decision) — "+m.ConfirmPath)
	m.ConfirmID = ""
	m.ConfirmPath = ""
	m.ConfirmDiff = ""
	m.ConfirmType = ""
	m.ConfirmPaths = nil
	m._cachedViewportContent = ""
	m._stableHistoryContent = ""
	return m
}

func (m Model) handlePhase(msg MsgFromEngine) (Model, tea.Cmd, bool) {
	prevPhase := m.Phase
	if msg.Value != "" {
		m.Phase = Phase(msg.Value)
	}
	newPhase := m.Phase

	// A genuine phase change is meaningful activity: it re-opens the Thought
	// latch so the next reasoning chain may surface once as orientation.
	// Late/chained reasoning never stacks — only the latch owner re-arms.
	if newPhase != prevPhase {
		m._suppressThought = false
	}

	if newPhase == PhasePlan && prevPhase != PhasePlan {
		m.ExecTree.StartPlan()
		m.ExecTree.AddPlanStep("reasoning")
		planEv := m.Timeline.Add(components.TimelineEvent{
			Type:      components.EventPlan,
			Status:    components.StatusRunning,
			Title:     "Planning started",
			Timestamp: time.Now(),
		})
		m._phasePlanID = planEv.ID
	}
	if newPhase == PhaseExecute && prevPhase != PhaseExecute {
		if prevPhase == PhasePlan && m._phasePlanID != "" {
			m.Timeline.UpdateByID(m._phasePlanID, components.StatusSuccess, "Planning completed")
		}
		m.IsThinking = false
		m.ScrollLock = false
		m.ExecTree.StartExecution()
		m._viewportDirty = true
		execEv := m.Timeline.Add(components.TimelineEvent{
			Type:      components.EventExecution,
			Status:    components.StatusRunning,
			Title:     "Execution started",
			Timestamp: time.Now(),
		})
		m._phaseExecID = execEv.ID
	}
	if newPhase == PhaseIdle && prevPhase == PhaseExecute && m._phaseExecID != "" {
		m.Timeline.UpdateByID(m._phaseExecID, components.StatusSuccess, "Execution completed")
	}

	// Rift: workspace cleanup is handled by user approval (y/n) and program shutdown only.
	// Phase transitions cannot be used because the engine emits phase:IDLE after every tool round,
	// not just at task completion — auto-discard would delete the clone mid-task.

	return m, nil, false
}

func (m Model) handleToolStart(msg MsgFromEngine) (Model, tea.Cmd, bool) {
	// A tool boundary ends the provider round that requested it — commit the
	// round's KYREX message + Thought before surfacing the tool, so the
	// transcript stays interleaved: KYREX → Thought → tool → …
	m = m.commitRound()
	// Tool activity is meaningful: it re-opens the Thought latch so the next
	// post-tool reasoning chain may surface as its own orientation Thought
	// (Thought → tool → Thought → tool rhythm), rather than staying suppressed.
	m._suppressThought = false
	m.CurrentTool = msg.Name
	m.ToolArgs = humanReadableTitle(msg.Name, msg.Args)
	// Track files read or edited this session for the active files sidebar
	if msg.Name == "read_local_file" || msg.Name == "edit_file" {
		if argMap, ok := msg.Args.(map[string]interface{}); ok {
			if p, ok := argMap["path"].(string); ok && p != "" {
				// Deduplicate: remove existing entry then prepend
				filtered := make([]string, 0, len(m.ActiveFiles))
				for _, f := range m.ActiveFiles {
					if f != p {
						filtered = append(filtered, f)
					}
				}
				m.ActiveFiles = append([]string{p}, filtered...)
				if len(m.ActiveFiles) > 5 {
					m.ActiveFiles = m.ActiveFiles[:5]
				}
			}
		}
	}
	m._progressUpdateCount++
	m.ToolResult = ""
	m.Tools.Add(ToolEvent{
		ID:        fmt.Sprintf("%d", time.Now().UnixNano()),
		Name:      msg.Name,
		Args:      humanReadableTitle(msg.Name, msg.Args),
		State:     ToolStateRunning,
		StartTime: time.Now(),
	})

	toolID := msg.ID
	if toolID == "" {
		toolID = fmt.Sprintf("tool_%d", time.Now().UnixNano())
	}
	m._lastToolID = toolID
	m.Timeline.Add(components.TimelineEvent{
		ID:        toolID,
		Type:      components.EventTool,
		Status:    components.StatusRunning,
		Title:     humanReadableTitle(msg.Name, msg.Args),
		Timestamp: time.Now(),
	})

	m._viewportDirty = true

	return m, nil, false
}

func (m Model) handleToolResult(msg MsgFromEngine) (Model, tea.Cmd, bool) {
	m.ToolResult = "OK"
	resultStr := ""
	hasErr := false
	if msg.Result != nil {
		if resMap, ok := msg.Result.(map[string]interface{}); ok {
			if err, ok := resMap["error"]; ok && err != nil {
				m.ToolResult = fmt.Sprintf("ERR: %v", err)
				resultStr = fmt.Sprintf("ERR: %v", err)
				hasErr = true
				m.Tools.UpdateLast(ToolStateFailed, resultStr)
			}
		}
	}
	if m.ToolResult == "OK" {
		m.Tools.UpdateLast(ToolStateSuccess, "OK")
	}

	toolID := msg.ID
	if toolID == "" {
		toolID = m._lastToolID
	}
	if toolID != "" {
		status := components.StatusSuccess
		if hasErr {
			status = components.StatusFailed
		}
		m.Timeline.UpdateByID(toolID, status, resultStr)
	}

	m._viewportDirty = true
	return m, nil, false
}

func (m Model) handleConfirmRequest(msg MsgFromEngine) (Model, tea.Cmd, bool) {
	// A new approval REPLACES the live presentation. The superseded engine
	// gate — if one is still pending — receives an explicit denial so it is
	// never left blocked on its approval wait (in practice a single engine
	// turn cannot emit two overlapping gates; this covers re-entrant and
	// multi-lane frames defensively).
	newID := msg.ID
	if newID == "" {
		newID = msg.RequestID
	}
	if m.ConfirmID != "" && m.ConfirmID != newID {
		if m.SendFunc != nil {
			_ = m.SendFunc(map[string]interface{}{
				"type":     "confirm_response",
				"id":       m.ConfirmID,
				"approved": false,
			})
		}
		m.Timeline.UpdateByID(m.ConfirmID, components.StatusWarning, "Dismissed (superseded) — "+m.ConfirmPath)
	}
	m.ConfirmID = newID
	m.ConfirmPath = msg.Path
	m.ConfirmDiff = msg.Diff
	m.ConfirmType = msg.Value  // "deletion" for rm/rmdir gates, "" for edit/diff gates
	m.ConfirmPaths = msg.Paths // real resolved deletion targets; display text stays in ConfirmPath
	m.IsThinking = false

	confirmTitle := "Diff — " + m.ConfirmPath
	if m.ConfirmType == "deletion" {
		confirmTitle = "Delete — " + m.ConfirmPath
	}
	m.Timeline.Add(components.TimelineEvent{
		ID:        m.ConfirmID,
		Type:      components.EventApproval,
		Status:    components.StatusWarning,
		Title:     confirmTitle,
		Timestamp: time.Now(),
	})

	// Deletion confirmations are NEVER auto-approved: "rm" may only execute
	// after an explicit human y/n decision. Auto-approve remains available for
	// non-destructive gates (edits/diffs).
	if m.AutoApprove && m.ConfirmType != "deletion" {
		return m, autoApproveCmd(m.AutoApproveDelay, m.ConfirmID), false
	}
	return m, nil, false
}

func (m Model) handleDiff(msg MsgFromEngine) (Model, tea.Cmd, bool) {
	diffID := msg.ID
	if diffID == "" {
		diffID = fmt.Sprintf("diff_%d", time.Now().UnixNano())
	}
	diffPath := msg.Path
	diffStr := msg.Diff

	// Clear stale reasoning text so the diff pane renders cleanly
	m.Reasoning = ""

	if diffStr != "" {
		block := components.ParseUnifiedDiff(diffStr, diffID)
		if diffPath != "" && block.FilePath == "" {
			block.FilePath = diffPath
		}

		replaced := false
		for i, existing := range m.DiffBlocks {
			if existing.ID == diffID {
				m.DiffBlocks[i] = *block
				replaced = true
				break
			}
		}
		if !replaced {
			m.DiffBlocks = append(m.DiffBlocks, *block)
		}
		m.ActiveDiffID = diffID

		// Store the rendered diff content in history for the packet architecture
		renderedDiff := components.RenderSideBySideStream([]components.DiffBlock{*block}, 80)
		m.History = append(m.History, "_DiffContent:_\n"+renderedDiff)
	}

	m._viewportDirty = true

	return m, nil, false
}

func (m Model) handleError(msg MsgFromEngine) (Model, tea.Cmd, bool) {
	m.IsThinking = false
	m.History = append(m.History, "ERROR: "+msg.Content)
	m._cachedViewportContent = ""
	m._stableHistoryContent = "" // invalidate stable cache — history just changed
	m._viewportDirty = true
	m.ScrollLock = false

	errTitle := msg.Content
	if len(errTitle) > 30 {
		errTitle = errTitle[:29] + "…"
	}
	m.Timeline.Add(components.TimelineEvent{
		Type:      components.EventError,
		Status:    components.StatusFailed,
		Title:     errTitle,
		Timestamp: time.Now(),
	})

	return m, nil, false
}

func (m Model) handleSessionState(msg MsgFromEngine) (Model, tea.Cmd, bool) {
	if msg.Model != "" {
		m.LLMInfo = fmt.Sprintf("%s (%s)", msg.Model, msg.Provider)
	}
	if msg.Context != "" {
		m.Context = msg.Context
	}
	if msg.Files != nil {
		if filesMap, ok := msg.Files.(map[string]interface{}); ok {
			if dirs, ok := filesMap["dirs"].([]interface{}); ok {
				m.WorkspaceDirs = make([]string, len(dirs))
				for i, d := range dirs {
					m.WorkspaceDirs[i] = fmt.Sprint(d)
				}
			}
			if files, ok := filesMap["files"].([]interface{}); ok {
				m.WorkspaceFiles = make([]string, len(files))
				for i, f := range files {
					m.WorkspaceFiles[i] = fmt.Sprint(f)
				}
			}
		}
	}
	if msg.SessionBranch != "" {
		m.SessionBranch = msg.SessionBranch
	}
	return m, nil, false
}
