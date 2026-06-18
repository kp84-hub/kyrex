package tui

import (
	"fmt"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/kp84-hub/kx/tui/components"
)

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
	return m, nil, true
}

func (m Model) handleChatDone(msg MsgFromEngine) (Model, tea.Cmd, bool) {
	// Cancel any pending coalesce tick — chat_done does an immediate flush
	m._tokenCoalescePending = false
	finalRes := msg.Content
	if finalRes == "" {
		finalRes = m.CurrToken
	}
	if finalRes == "" && msg.Result != nil {
		if resStr, ok := msg.Result.(string); ok && resStr != "" {
			finalRes = resStr
		}
	}

	reasoningText := m.Reasoning
	if reasoningText == "" && msg.Reasoning != "" {
		reasoningText = msg.Reasoning
	}
	if finalRes == "" && reasoningText != "" {
		finalRes = reasoningText
	}

	m.IsThinking = false

	if reasoningText != "" {
		m.History = append(m.History, "_Thinking:_\n"+reasoningText)
	}

	m._cachedViewportContent = ""
	m._stableHistoryContent = "" // invalidate stable cache — history just changed
	m._viewportDirty = true
	m.Reasoning = ""

	if finalRes != "" {
		m.History = append(m.History, "_Overview:_\n"+finalRes)
	}
	m.CurrToken = ""

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

	// Clear diff/confirm state so the overview renders instead of stale panes
	m.DiffBlocks = nil
	m.ActiveDiffID = ""
	m.ConfirmID = ""
	m.ConfirmPath = ""
	m.ConfirmDiff = ""

	return m, nil, true
}

func (m Model) handlePhase(msg MsgFromEngine) (Model, tea.Cmd, bool) {
	prevPhase := m.Phase
	if msg.Value != "" {
		m.Phase = Phase(msg.Value)
	}
	newPhase := m.Phase

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

	return m, nil, false
}

func (m Model) handleToolStart(msg MsgFromEngine) (Model, tea.Cmd, bool) {
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
	m.ConfirmID = msg.ID
	if m.ConfirmID == "" {
		m.ConfirmID = msg.RequestID
	}
	m.ConfirmPath = msg.Path
	m.ConfirmDiff = msg.Diff
	m.IsThinking = false

	m.Timeline.Add(components.TimelineEvent{
		ID:        m.ConfirmID,
		Type:      components.EventApproval,
		Status:    components.StatusWarning,
		Title:     "Diff — " + m.ConfirmPath,
		Timestamp: time.Now(),
	})

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
