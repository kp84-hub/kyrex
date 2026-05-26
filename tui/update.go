package tui

import (
	"fmt"
	"strings"
	"time"

	"github.com/atotto/clipboard"
	"github.com/charmbracelet/bubbles/textarea"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/kp84-hub/kx/tui/components"
)

type MsgFromEngine struct {
	Type      string
	ID        string
	Content   string
	Phase     Phase
	Name      string
	Args      interface{}
	Result    interface{}
	Value     string
	Model     string
	Provider  string
	Context   string
	Files     []string
	Stdout    string
	Reasoning string
	RequestID string
	Path      string
	Diff      string
	Todos     []string
	SessionBranch string
	Mode          string
}

type TickMsg time.Time

func Tick() tea.Cmd {
	return tea.Tick(time.Second, func(t time.Time) tea.Msg {
		return TickMsg(t)
	})
}

type FastTickMsg time.Time

func FastTick() tea.Cmd {
	return tea.Tick(150*time.Millisecond, func(t time.Time) tea.Msg {
		return FastTickMsg(t)
	})
}

func (m Model) Init() tea.Cmd {
	return tea.Batch(textarea.Blink, Tick(), FastTick())
}

func (m Model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	var (
		tiCmd tea.Cmd
		vpCmd tea.Cmd
		cmds  []tea.Cmd
	)

	switch msg := msg.(type) {
	case tea.KeyMsg:
		// --- APP-LEVEL HOTKEYS (work regardless of input focus) ---
		switch msg.String() {
		case "ctrl+t", "ctrl+g":
			m.MouseEnabled = !m.MouseEnabled
			if m.MouseEnabled {
				m.Toast = "MOUSE mode — full UI"
			} else {
				m.Toast = "DRAG mode — select text with mouse to copy"
			}
			m.ToastEnd = time.Now().Add(2 * time.Second)
			return m, nil
		case "esc":
			if m.IsThinking {
				if m.SendFunc != nil {
					m.SendFunc(map[string]interface{}{
						"type": "interrupt",
					})
				}
				m.Toast = "Interrupting..."
				m.ToastEnd = time.Now().Add(2 * time.Second)
			}
			return m, nil
		}

		// Handle confirmation gate shortcuts
		if m.ConfirmID != "" {
			switch msg.String() {
		case "y", "Y":
			if m.SendFunc != nil {
				m.SendFunc(map[string]interface{}{
					"type":     "confirm_response",
					"id":       m.ConfirmID,
					"approved": true,
				})
			}
			m.History = append(m.History, "󰄬  Approved change to: "+m.ConfirmPath)
			m.Timeline.UpdateByID(m.ConfirmID, components.StatusSuccess, "Approved — "+m.ConfirmPath)
			m.ConfirmID = ""
				m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
				return m, nil
		case "n", "N":
			if m.SendFunc != nil {
				m.SendFunc(map[string]interface{}{
					"type":     "confirm_response",
					"id":       m.ConfirmID,
					"approved": false,
				})
			}
			m.History = append(m.History, "󰅙  Rejected change to: "+m.ConfirmPath)
			m.Timeline.UpdateByID(m.ConfirmID, components.StatusWarning, "Rejected — "+m.ConfirmPath)
			m.ConfirmID = ""
				m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
				return m, nil
			}
		}

		switch msg.Type {
		case tea.KeyCtrlC:
			return m, tea.Quit
		case tea.KeyF9: // Toggle Mode
			if m.SendFunc != nil {
				m.SendFunc(map[string]string{
					"type":    "command",
					"content": "/mode",
				})
			}
			return m, nil
		case tea.KeyCtrlB: // Toggle Sidebar
			m.ShowSidebar = !m.ShowSidebar
			// Trigger a resize calculation
			return m, func() tea.Msg {
				return tea.WindowSizeMsg{Width: m.Width, Height: m.Height}
			}
		case tea.KeyCtrlY: // Copy last assistant response
			if len(m.History) > 0 {
				// If it's a reasoning or log block, skip it and go further back
				idx := len(m.History) - 1
				for idx >= 0 && (strings.HasPrefix(m.History[idx], "_Thinking:_") || 
								 strings.HasPrefix(m.History[idx], "_Logs:_") || 
								 strings.HasPrefix(m.History[idx], "> ")) {
					idx--
				}
				if idx >= 0 {
					clipboard.WriteAll(m.History[idx])
				} else {
					// Fallback to whatever the last thing was
					clipboard.WriteAll(m.History[len(m.History)-1])
				}
			}
			return m, nil
		case tea.KeyEnter, tea.KeyCtrlJ: // Submit on Enter or Ctrl+J
			input := strings.TrimSpace(m.Textarea.Value())
			if input != "" {
				m.Textarea.Reset()
				
							// Handle /clear locally for UI history
			if input == "/clear" {
				// Interrupt any in-flight engine request
				if m.SendFunc != nil {
					m.SendFunc(map[string]interface{}{
						"type": "interrupt",
					})
				}
				m.History = nil
				m.CurrToken = ""
				m.Reasoning = ""
				m.Timeline.Clear()
				m.MissionSummary = ""
				m.ExecTree.Clear()
				m.Tools = NewToolTelemetry(50)
				m.CurrentTool = ""
				m.ToolArgs = ""
				m.ToolResult = ""
				m.IsThinking = false
				m.Timer = 0
				m.ScrollLock = false
				m.ConfirmID = ""
				m.ConfirmPath = ""
				m.ConfirmDiff = ""
				m._phasePlanID = ""
				m._phaseExecID = ""
				m._lastToolID = ""
				m._cachedViewportContent = ""
				m._viewportDirty = false
				m._suppressEngine = true
				m.Viewport.SetContent("")
				return m, nil
			}

				// Mobile-friendly toggle commands
				if input == ":sidebar" || input == ":w" {
					m.ShowSidebar = !m.ShowSidebar
					return m, func() tea.Msg {
						return tea.WindowSizeMsg{Width: m.Width, Height: m.Height}
					}
				}

				if m.SendFunc != nil {
					// Re-enable engine messages for the new request
					m._suppressEngine = false
					// Commit any live answer from previous turn
					if m.CurrToken != "" {
						m.History = append(m.History, "_Overview:_\n"+m.CurrToken)
						m.CurrToken = ""
					}
					msgType := "chat"
					if strings.HasPrefix(input, "/") {
						msgType = "command"
					}
					m.SendFunc(map[string]string{
						"type":    msgType,
						"content": input,
					})
				}
					m.History = append(m.History, "> "+input) // Show user prompt
		m.CurrToken = ""   // Clear residual tokens (e.g. from commands)
		m.Reasoning = ""
		m.IsThinking = false
		m.Timer = 0
		m.ScrollLock = false
		m.Timeline.Clear()
		m.MissionSummary = ""
		m.ExecTree.Clear()
		m.Tools = NewToolTelemetry(50)
		m.CurrentTool = ""
		m.ToolArgs = ""
		m.ToolResult = ""
		m.ConfirmID = ""
		m.ConfirmPath = ""
		m.ConfirmDiff = ""
		m._phasePlanID = ""
		m._phaseExecID = ""
		m._lastToolID = ""
		m._cachedViewportContent = ""
		m._viewportDirty = false
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
				m.Viewport.GotoBottom()
			}
			return m, nil
		}

	case tea.MouseMsg:
		// Only handle selection during MOUSE mode (full UI) with left button
		if !m.MouseEnabled {
			break
		}

		// Calculate viewport screen origin (matches view.go layout)
		showSidebar := m.ShowSidebar
		if m.ConfirmID != "" {
			showSidebar = false
		}
		sidebarWidth := 0
		if showSidebar {
			sidebarWidth = 25
			if sidebarWidth > m.Width/3 {
				sidebarWidth = m.Width / 3
			}
		}
		vpStartX := 0
		if showSidebar {
			vpStartX = sidebarWidth + 1
		}
		vpStartY := 0
		if !m.MouseEnabled {
			vpStartY = 0 // DRAG mode starts at top
		}

		// Convert screen coordinates to viewport-local
		localX := msg.X - vpStartX
		localY := msg.Y - vpStartY

		// Convert viewport-local to absolute buffer position
		absLine := localY + m.Viewport.YOffset

		if msg.Button == tea.MouseButtonLeft {
			switch msg.Action {
			case tea.MouseActionPress:
				if localX >= 0 && localX < m.Viewport.Width &&
					localY >= 0 && localY < m.Viewport.Height {
					m.Selecting = true
					m.SelectStart = SelectionPoint{Line: absLine, Col: localX}
					m.SelectEnd = SelectionPoint{Line: absLine, Col: localX}
					m.AutoScrollDir = 0
				}
			case tea.MouseActionMotion:
				if m.Selecting {
					// Clamp to viewport
					if localX < 0 {
						localX = 0
					}
					if localX >= m.Viewport.Width {
						localX = m.Viewport.Width - 1
					}
					if localY < 0 {
						localY = 0
					}
					if localY >= m.Viewport.Height {
						localY = m.Viewport.Height - 1
					}
					m.SelectEnd = SelectionPoint{Line: absLine, Col: localX}

					// Regenerate viewport content so inline selection highlights update live
					m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))

					// Auto-scroll edge detection
					if localY >= m.Viewport.Height-1 && !m.Viewport.AtBottom() {
						m.AutoScrollDir = 1 // scroll down
					} else if localY <= 0 && m.Viewport.YOffset > 0 {
						m.AutoScrollDir = -1 // scroll up
					} else {
						m.AutoScrollDir = 0
					}
				}
			default: // Release or other action
				if m.Selecting {
					m.Selecting = false
					m.AutoScrollDir = 0
					selectedText := m.GetSelectedText()
					if selectedText != "" {
						clipboard.WriteAll(selectedText)
						m.Toast = "Copied to clipboard"
						m.ToastEnd = time.Now().Add(2 * time.Second)
					}
				}
			}
		} else if m.Selecting {
			// Any non-left button cancels selection
			m.Selecting = false
			m.AutoScrollDir = 0
		}

	case tea.WindowSizeMsg:
		// Dynamic check: if terminal width has physically changed (or on boot) and is narrow (< 120), hide sidebar by default.
		// Respect manual toggles (Ctrl+B, :sidebar, :w) which trigger synthetic WindowSizeMsg with unchanged width.
		if msg.Width != m.Width && msg.Width < 120 {
			m.ShowSidebar = false
		}

		m.Width = msg.Width
		m.Height = msg.Height
		
		// Recalculate component dimensions immediately using responsive logic
		showSidebar := m.ShowSidebar
		if m.ConfirmID != "" {
			showSidebar = false
		}

		sidebarWidth := 0
		if showSidebar {
			sidebarWidth = 25
			if sidebarWidth > m.Width/3 {
				sidebarWidth = m.Width / 3
			}
		}
		mainWidth := m.Width - sidebarWidth - 1
		if !showSidebar {
			mainWidth = m.Width
		}
		if mainWidth < 1 {
			mainWidth = 1
		}
		footerHeight := 1
		textareaHeight := 1
		viewportHeight := m.Height - textareaHeight - footerHeight - 1
		if viewportHeight < 1 {
			viewportHeight = 1
		}

		vpW := mainWidth - 2
		if vpW < 1 {
			vpW = 1
		}
		m.Viewport.Width = vpW
		m.Viewport.Height = viewportHeight
		m.Textarea.SetWidth(mainWidth - 2)
		m.Textarea.SetHeight(1)

	case FastTickMsg:
		// Flush viewport if dirty from token/reasoning accumulation
		if m._viewportDirty && time.Since(m._lastViewportFlush) > 120*time.Millisecond {
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			if !m.ScrollLock {
				m.Viewport.GotoBottom()
			}
			m._lastViewportFlush = time.Now()
			m._viewportDirty = false
		}
		// Continuous auto-scroll during selection
		if m.Selecting && m.AutoScrollDir != 0 {
			if m.AutoScrollDir > 0 {
				m.Viewport.LineDown(3)
			} else {
				m.Viewport.LineUp(3)
			}
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		}
		cmds = append(cmds, FastTick())

	case TickMsg:
		if m.IsThinking {
			m.Timer++
		}
		// Flush viewport if dirty from token/reasoning accumulation
		if m._viewportDirty && time.Since(m._lastViewportFlush) > 120*time.Millisecond {
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			if !m.ScrollLock {
				m.Viewport.GotoBottom()
			}
			m._lastViewportFlush = time.Now()
			m._viewportDirty = false
		}
		if m.Toast != "" && time.Now().After(m.ToastEnd) {
			m.Toast = ""
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		}
		cmds = append(cmds, Tick())

	case MsgFromEngine:
		// Drop stale engine messages after clear/reset (except session_state)
		if m._suppressEngine && msg.Type != "session_state" {
			return m, nil
		}
		switch msg.Type {
		case "token", "content":
			m.IsThinking = false
			m.CurrToken += msg.Content
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			if !m.ScrollLock {
				m.Viewport.GotoBottom()
			}
			m._lastViewportFlush = time.Now()
			m._viewportDirty = false
		case "log":
			m.History = append(m.History, "_Logs:_\n"+msg.Content)
			m._viewportDirty = true
		case "reasoning":
			m.IsThinking = true
			if msg.Content != "" {
				m.Reasoning += msg.Content
			} else if msg.Reasoning != "" {
				m.Reasoning += msg.Reasoning
			}
			m._viewportDirty = true
			if time.Since(m._lastViewportFlush) > 80*time.Millisecond {
				m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
				if !m.ScrollLock {
					m.Viewport.GotoBottom()
				}
				m._lastViewportFlush = time.Now()
				m._viewportDirty = false
			}
		case "chat_done":
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

			m.IsThinking = false

			if reasoningText != "" {
				m.History = append(m.History, "_Thinking:_\n"+reasoningText)
			}

			m._cachedViewportContent = ""
			m._viewportDirty = true
			m.Reasoning = ""

			if finalRes != "" {
				m.History = append(m.History, "_Overview:_\n"+finalRes)
			}
			m.CurrToken = ""

			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
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

		case "phase":
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
				m.Viewport.GotoBottom()
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

		case "tool_start":
			m.CurrentTool = msg.Name
			m.ToolArgs = humanReadableTitle(msg.Name, msg.Args)
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

			if !m.ScrollLock {
				m.Viewport.GotoBottom()
			}

		case "tool_result":
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

		case "confirm_request":
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

		case "error":
			m.IsThinking = false
			m.History = append(m.History, "ERROR: "+msg.Content)
			m._cachedViewportContent = ""
			m._viewportDirty = true
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			m.ScrollLock = false
			m.Viewport.GotoBottom()

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

		case "session_state":
			if msg.Model != "" {
				m.LLMInfo = fmt.Sprintf("%s (%s)", msg.Model, msg.Provider)
			}
			if msg.Context != "" {
				m.Context = msg.Context
			}
			if len(msg.Files) > 0 {
				m.ProjectFiles = msg.Files
			}
			if msg.SessionBranch != "" {
				m.SessionBranch = msg.SessionBranch
			}
			if msg.Mode != "" {
				m.Mode = msg.Mode
			}
		}
	}

	// Only pass keyboard messages to textarea — mouse events cause phantom line stacking
	switch msg.(type) {
	case tea.KeyMsg:
		m.Textarea, tiCmd = m.Textarea.Update(msg)
	default:
		tiCmd = nil
	}
	m.Viewport, vpCmd = m.Viewport.Update(msg)
	cmds = append(cmds, tiCmd, vpCmd)

	if m.Viewport.AtBottom() {
		m.ScrollLock = false
	} else if _, ok := msg.(tea.KeyMsg); ok {
		m.ScrollLock = true
	}

	return m, tea.Batch(cmds...)
}

func (m Model) GetSelectedText() string {
	if m.SelectStart == m.SelectEnd {
		return ""
	}

	content := m.HistoryContentClean(m.Viewport.Width)
	lines := strings.Split(content, "\n")

	start := m.SelectStart
	end := m.SelectEnd

	// Normalize: start should be before end
	if start.Line > end.Line || (start.Line == end.Line && start.Col > end.Col) {
		start, end = end, start
	}

	var result []string
	for lineIdx := start.Line; lineIdx <= end.Line; lineIdx++ {
		if lineIdx < 0 || lineIdx >= len(lines) {
			continue
		}
		line := lines[lineIdx]
		runes := []rune(line)

		colStart := 0
		if lineIdx == start.Line {
			colStart = start.Col
		}
		colEnd := len(runes)
		if lineIdx == end.Line {
			colEnd = end.Col
		}

		if colStart < 0 {
			colStart = 0
		}
		if colStart > len(runes) {
			colStart = len(runes)
		}
		if colEnd < 0 {
			colEnd = 0
		}
		if colEnd > len(runes) {
			colEnd = len(runes)
		}
		if colEnd < colStart {
			colEnd = colStart
		}

		result = append(result, string(runes[colStart:colEnd]))
	}

	return strings.Join(result, "\n")
}

func (m Model) HistoryContentClean(width int) string {
	content := ""
	style := lipgloss.NewStyle().Width(width)
	assistantStyle := lipgloss.NewStyle().Foreground(fg).Width(width)
	thinkingStyleClean := lipgloss.NewStyle().Foreground(thinkingC).Width(width)
	logsStyleClean := lipgloss.NewStyle().Foreground(subtle).Width(width)
	
	for _, h := range m.History {
		if strings.HasPrefix(h, "> ") {
			content += "> You\n" + style.Render(h[2:]) + "\n\n"
		} else if strings.HasPrefix(h, "_Thinking:_") {
			inner := strings.TrimPrefix(h, "_Thinking:_")
			content += "[Thinking]\n" + thinkingStyleClean.Render(inner) + "\n" + separatorStyle.Render("────────────────────────────────────────────────") + "\n\n"
		} else if strings.HasPrefix(h, "_Logs:_") {
			inner := strings.TrimPrefix(h, "_Logs:_")
			content += "[Logs]\n" + logsStyleClean.Render(inner) + "\n\n"
		} else if strings.HasPrefix(h, "_Overview:_") {
			inner := strings.TrimPrefix(h, "_Overview:_")
			content += "Overview\n" + assistantStyle.Render(inner) + "\n\n"
		} else {
			content += "Kyrex\n" + assistantStyle.Render(h) + "\n\n"
		}
	}
	// Add current streaming content
	if m.Reasoning != "" {
		content += "[Thinking]\n" + thinkingStyleClean.Render(m.Reasoning) + "\n\n"
	}
	if m.CurrToken != "" {
		content += "KYREX\n" + assistantStyle.Render(m.CurrToken) + "\n\n"
	}
	
	return content
}

// HistoryContent builds the history buffer with selection highlights baked in via absolute indexing.
func (m Model) HistoryContent(width int) string {
	selStart, selEnd := m.SelectStart.Line, m.SelectEnd.Line
	if selStart > selEnd {
		selStart, selEnd = selEnd, selStart
	}

	style := lipgloss.NewStyle().Width(width)
	var content strings.Builder
	absLine := 0
	selecting := m.Selecting

	emit := func(line string) {
		if selecting && absLine >= selStart && absLine <= selEnd {
			content.WriteString("\x1b[7m" + line + "\x1b[27m\n")
		} else {
			content.WriteString(line + "\n")
		}
		absLine++
	}

	emitBlock := func(lines []string) {
		for _, l := range lines {
			emit(l)
		}
	}

	for _, h := range m.History {
		if strings.HasPrefix(h, "> ") {
			emit(lipgloss.NewStyle().Foreground(accent).Bold(true).Render("> You"))
			emitBlock(strings.Split(style.Render(h[2:]), "\n"))
			content.WriteString("\n")
			absLine++
		} else if strings.HasPrefix(h, "_Thinking:_") {
			inner := strings.TrimPrefix(h, "_Thinking:_")
			emit(lipgloss.NewStyle().Foreground(thinkingC).Italic(true).Render("󰉋  Thought"))
			emitBlock(strings.Split(thinkingStyle.Width(width-2).Render(inner), "\n"))
			emit(separatorStyle.Width(width).Render("────────────────────────────────────────────────"))
			content.WriteString("\n")
			absLine++
		} else if strings.HasPrefix(h, "_Logs:_") {
			inner := strings.TrimPrefix(h, "_Logs:_")
			emit(lipgloss.NewStyle().Foreground(red).Bold(true).Render("[Logs]"))
			emitBlock(strings.Split(lipgloss.NewStyle().Foreground(subtle).Width(width).Render(inner), "\n"))
			content.WriteString("\n")
			absLine++
		} else if strings.HasPrefix(h, "_Overview:_") {
			inner := strings.TrimPrefix(h, "_Overview:_")
			emit(lipgloss.NewStyle().Foreground(green).Bold(true).Render("󰄬  Overview"))
			emitBlock(strings.Split(overviewStyle.Width(width).Render(inner), "\n"))
			content.WriteString("\n")
			absLine++
		} else {
			emit(lipgloss.NewStyle().Foreground(purple).Bold(true).Render("KYREX"))
			emitBlock(strings.Split(lipgloss.NewStyle().Foreground(fg).Width(width).Render(h), "\n"))
			content.WriteString("\n")
			absLine++
		}
	}

	return content.String()
}

// ReasoningContent builds the active reasoning block with selection highlights baked in via absolute indexing.
func (m Model) ReasoningContent(width int) string {
	if m.Reasoning == "" {
		return ""
	}

	selStart, selEnd := m.SelectStart.Line, m.SelectEnd.Line
	if selStart > selEnd {
		selStart, selEnd = selEnd, selStart
	}

	selecting := m.Selecting
	// Calculate the absolute line offset so selection indices remain contiguous
	absLine := m.countHistoryLines(width)

	var content strings.Builder

	emit := func(line string) {
		if selecting && absLine >= selStart && absLine <= selEnd {
			content.WriteString("\x1b[7m" + line + "\x1b[27m\n")
		} else {
			content.WriteString(line + "\n")
		}
		absLine++
	}

	emit(lipgloss.NewStyle().Foreground(thinkingC).Italic(true).Render("󰉋  Thought"))
	for _, tl := range strings.Split(thinkingStyle.Width(width-2).Render(m.Reasoning), "\n") {
		emit(tl)
	}
	emit(separatorStyle.Width(width).Render("────────────────────────────────────────────────"))
	content.WriteString("\n")
	absLine++

	return content.String()
}

func (m Model) countHistoryLines(width int) int {
	style := lipgloss.NewStyle().Width(width)
	count := 0
	for _, h := range m.History {
		if strings.HasPrefix(h, "> ") {
			count++                                                                         // "> You" header
			count += len(strings.Split(style.Render(h[2:]), "\n"))                          // content lines
			count++                                                                         // blank spacer
		} else if strings.HasPrefix(h, "_Thinking:_") {
			inner := strings.TrimPrefix(h, "_Thinking:_")
			count++
			count += len(strings.Split(thinkingStyle.Width(width-2).Render(inner), "\n"))
			count++ // separator
			count++
		} else if strings.HasPrefix(h, "_Logs:_") {
			inner := strings.TrimPrefix(h, "_Logs:_")
			count++
			count += len(strings.Split(lipgloss.NewStyle().Foreground(subtle).Width(width).Render(inner), "\n"))
			count++
		} else if strings.HasPrefix(h, "_Overview:_") {
			inner := strings.TrimPrefix(h, "_Overview:_")
			count++
			count += len(strings.Split(lipgloss.NewStyle().Foreground(fg).Width(width).Render(inner), "\n"))
			count++
		} else {
			count++
			count += len(strings.Split(style.Render(h), "\n"))
			count++
		}
	}
	return count
}

// FullViewportContent builds the complete viewport buffer with selection highlights applied.
func (m Model) FullViewportContent(width int) string {
	// Check cache (Phase 6: dirty-region optimization)
	if m._cachedViewportContent != "" && m._cachedWidth == width &&
		!m._viewportDirty && !m.Selecting && m.Reasoning == "" && m.CurrToken == "" {
		return m._cachedViewportContent
	}

	var content strings.Builder

	// 1. Grab the rendered history (which already has line highlights baked in via absolute indexing)
	content.WriteString(m.HistoryContent(width))

	// 2. Tool telemetry feed (Phase 4: tool observability panel)
	telemetry := m.RenderToolTelemetry(width)
	if telemetry != "" {
		content.WriteString(telemetryStyle.Width(width).Render(telemetry) + "\n")
	}

	// 3. Append active reasoning (if the agent is currently thinking)
	if m.Reasoning != "" {
		content.WriteString(m.ReasoningContent(width))
	}

	// 4. Append active streaming tokens (if the agent is currently typing)
	if m.CurrToken != "" {
		content.WriteString(lipgloss.NewStyle().Foreground(purple).Bold(true).Render("KYREX") + "\n")
		content.WriteString(lipgloss.NewStyle().Foreground(fg).Width(width).Render(m.CurrToken) + "\n")
	}

	// 5. Append mission summary when present
	if m.MissionSummary != "" {
		content.WriteString(missionSummaryStyle.Width(width).Render(m.MissionSummary) + "\n")
	}

	result := content.String()

	// Update cache when history changes
	if !m.Selecting && m.Reasoning == "" && m.CurrToken == "" {
		m._cachedViewportContent = result
		m._cachedWidth = width
	}

	return result
}

func (m Model) generateMissionSummary() string {
	events := m.Timeline.EventsForCurrentTurn()
	if len(events) == 0 {
		return ""
	}

	toolCounts := make(map[string]int)
	var hasTools bool
	for _, e := range events {
		if e.Type == components.EventTool && e.Status == components.StatusSuccess {
			toolCounts[e.Title]++
			hasTools = true
		}
	}
	if !hasTools {
		return ""
	}

	header := lipgloss.NewStyle().Foreground(purple).Bold(true).Render("Mission Complete")
	var lines []string
	lines = append(lines, header)

	for title, count := range toolCounts {
		lines = append(lines, lipgloss.NewStyle().Foreground(green).Render("✓ "+title+" ×"+fmt.Sprintf("%d", count)))
	}

	return strings.Join(lines, "\n")
}

func humanReadableTitle(name string, args interface{}) string {
	argMap, ok := args.(map[string]interface{})
	if !ok {
		return name
	}

	switch name {
	case "read_local_file":
		if p, ok := argMap["path"].(string); ok {
			return "Read " + pathBasename(p)
		}
		return "Read file"
	case "list_local_files":
		if d, ok := argMap["directory"].(string); ok {
			return "List " + d
		}
		return "List files"
	case "search":
		if pat, ok := argMap["pattern"].(string); ok {
			s := pat
			if len(s) > 15 {
				s = s[:14] + "…"
			}
			return "Search \"" + s + "\""
		}
		return "Search"
	case "edit_file":
		if p, ok := argMap["path"].(string); ok {
			return "Edit " + pathBasename(p)
		}
		return "Edit file"
	case "write_file_with_gate":
		if p, ok := argMap["path"].(string); ok {
			return "Write " + pathBasename(p)
		}
		return "Write file"
	case "run_command":
		if c, ok := argMap["command"].(string); ok {
			cmdName := c
			if len(cmdName) > 20 {
				cmdName = cmdName[:19] + "…"
			}
			return "Run " + cmdName
		}
		return "Run command"
	case "query_memory":
		if q, ok := argMap["query"].(string); ok {
			s := q
			if len(s) > 12 {
				s = s[:11] + "…"
			}
			return "Memory \"" + s + "\""
		}
		return "Query memory"
	case "query_knowledge":
		if q, ok := argMap["query"].(string); ok {
			s := q
			if len(s) > 10 {
				s = s[:9] + "…"
			}
			return "Knowledge \"" + s + "\""
		}
		return "Query knowledge"
	default:
		return name
	}
}

func pathBasename(path string) string {
	parts := strings.Split(path, "/")
	for i := len(parts) - 1; i >= 0; i-- {
		if parts[i] != "" {
			return parts[i]
		}
	}
	return path
}
