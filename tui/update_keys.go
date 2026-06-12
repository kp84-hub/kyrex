package tui

import (
	"fmt"
	"strings"
	"time"

	"github.com/atotto/clipboard"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/kp84-hub/kx/tui/components"
)

// handleKeyMsg processes all keyboard input.
// Returns (model, cmd, handled) where handled=true means the caller should return immediately.
func (m Model) handleKeyMsg(msg tea.KeyMsg, prevKeyTime time.Time) (Model, tea.Cmd, bool) {
	// --- USAGE OVERLAY: intercept keys ---
	if m._usageOverlayActive {
		if msg.String() == "esc" || msg.String() == "q" {
			m._usageOverlayActive = false
			m._usageStats = nil
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			return m, nil, true
		}
		return m, nil, true
	}

	// --- MODEL PICKER: intercept keys ---
	if m._modelPickerActive {
		return m.handleModelPickerKey(msg)
	}

	// --- APP-LEVEL HOTKEYS (work regardless of input focus) ---
	switch msg.String() {
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
		return m, nil, true
	}

	// Handle confirmation gate shortcuts
	if m.ConfirmID != "" {
		return m.handleConfirmKey(msg)
	}

	switch msg.Type {
	case tea.KeyCtrlC:
		// Write metrics report on exit
		if m._metrics != nil {
			m._metrics.WriteReport("/tmp/kyrex_render_metrics.txt")
		}
		return m, tea.Quit, true
	case tea.KeyCtrlD:
		// Dump render metrics to file
		if m._metrics != nil {
			path := "/tmp/kyrex_render_metrics.txt"
			m._metrics.WriteReport(path)
			m.Toast = "Metrics → " + path
			m.ToastEnd = time.Now().Add(3 * time.Second)
		}
		return m, nil, true
	case tea.KeyF9: // Toggle Mode
		if m.SendFunc != nil {
			m.SendFunc(map[string]string{
				"type":    "command",
				"content": "/mode",
			})
		}
		return m, nil, true
	case tea.KeyCtrlB: // Toggle Sidebar
		m.ShowSidebar = !m.ShowSidebar
		return m, func() tea.Msg {
			return tea.WindowSizeMsg{Width: m.Width, Height: m.Height}
		}, true
	case tea.KeyCtrlY: // Copy last assistant response
		if len(m.History) > 0 {
			idx := len(m.History) - 1
			for idx >= 0 && (strings.HasPrefix(m.History[idx], "_Thinking:_") ||
				strings.HasPrefix(m.History[idx], "_Logs:_") ||
				strings.HasPrefix(m.History[idx], "> ")) {
				idx--
			}
			if idx >= 0 {
				clipboard.WriteAll(m.History[idx])
			} else {
				clipboard.WriteAll(m.History[len(m.History)-1])
			}
		}
		return m, nil, true
	case tea.KeyEnter, tea.KeyCtrlJ: // Submit on Enter or Ctrl+J
		return m.handleSubmit(msg, prevKeyTime)
	}

	return m, nil, false
}

func (m Model) handleModelPickerKey(msg tea.KeyMsg) (Model, tea.Cmd, bool) {
	switch msg.String() {
	case "esc", "q":
		m._modelPickerActive = false
		m._modelPickerItems = nil
		m._modelPickerInput = ""
		m._modelPickerIndex = 0
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	case "up":
		if len(m._modelPickerItems) > 0 {
			m._modelPickerIndex--
			if m._modelPickerIndex < 0 {
				m._modelPickerIndex = len(m._modelPickerItems) - 1
			}
			m._modelPickerInput = ""
		}
	case "down":
		if len(m._modelPickerItems) > 0 {
			m._modelPickerIndex++
			if m._modelPickerIndex >= len(m._modelPickerItems) {
				m._modelPickerIndex = 0
			}
			m._modelPickerInput = ""
		}
	case "enter":
		idx := m._modelPickerIndex
		if m._modelPickerInput != "" {
			idx = 0
			fmt.Sscanf(m._modelPickerInput, "%d", &idx)
			idx--
		}
		if idx >= 0 && idx < len(m._modelPickerItems) {
			selected := m._modelPickerItems[idx]
			m._modelPickerActive = false
			m._modelPickerItems = nil
			m._modelPickerInput = ""
			m._modelPickerIndex = 0
			if m.SendFunc != nil {
				m.SendFunc(map[string]string{
					"type":    "command",
					"content": "/model " + selected,
				})
			}
			m.History = append(m.History, "> /model "+selected)
			m.Toast = "Model: " + selected
			m.ToastEnd = time.Now().Add(2 * time.Second)
			m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
			return m, nil, true
		}
	case "backspace":
		if len(m._modelPickerInput) > 0 {
			m._modelPickerInput = m._modelPickerInput[:len(m._modelPickerInput)-1]
		}
	default:
		if len(msg.String()) == 1 {
			c := msg.String()[0]
			if c >= '0' && c <= '9' {
				m._modelPickerInput += string(c)
			}
		}
	}
	m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
	return m, nil, true
}

func (m Model) handleConfirmKey(msg tea.KeyMsg) (Model, tea.Cmd, bool) {
	switch msg.String() {
	case "y", "Y":
		if m.SendFunc != nil {
			m.SendFunc(map[string]interface{}{
				"type":     "confirm_response",
				"id":       m.ConfirmID,
				"approved": true,
			})
		}
		m.History = append(m.History, "\U000f012c  Approved change to: "+m.ConfirmPath)
		m.Timeline.UpdateByID(m.ConfirmID, components.StatusSuccess, "Approved — "+m.ConfirmPath)
		m.ConfirmID = ""
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	case "n", "N":
		if m.SendFunc != nil {
			m.SendFunc(map[string]interface{}{
				"type":     "confirm_response",
				"id":       m.ConfirmID,
				"approved": false,
			})
		}
		m.History = append(m.History, "\U000f0159  Rejected change to: "+m.ConfirmPath)
		m.Timeline.UpdateByID(m.ConfirmID, components.StatusWarning, "Rejected — "+m.ConfirmPath)
		m.ConfirmID = ""
		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		return m, nil, true
	}
	return m, nil, false
}

func (m Model) handleSubmit(msg tea.KeyMsg, prevKeyTime time.Time) (Model, tea.Cmd, bool) {
	// Paste burst detection: if Enter arrives < 25ms after the
	// previous keystroke, it's part of a paste — insert as a
	// literal newline instead of submitting.
	if msg.Type == tea.KeyEnter && time.Since(prevKeyTime) < 25*time.Millisecond {
		// Let it fall through to the textarea for InsertNewline
		return m, nil, false
	}

	input := strings.TrimSpace(m.Textarea.Value())
	if input == "" {
		return m, nil, true
	}

	// Commit any pending reasoning to history before starting new turn
	if m.Reasoning != "" {
		m.History = append(m.History, "_Thinking:_\n"+m.Reasoning)
		m.Reasoning = ""
	}
	// Also commit any live answer from previous turn
	if m.CurrToken != "" {
		m.History = append(m.History, "_Overview:_\n"+m.CurrToken)
		m.CurrToken = ""
	}

	// Clear viewport cache and refresh to show old content moved up
	m._cachedViewportContent = ""
	m._viewportDirty = true
	m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
	m.Viewport.GotoBottom()

	m.Textarea.Reset()

	// Handle /clear locally for UI history
	if input == "/clear" {
		if m.SendFunc != nil {
			m.SendFunc(map[string]interface{}{
				"type": "interrupt",
			})
		}
		if m.SendFunc != nil {
			m.SendFunc(map[string]string{
				"type":    "command",
				"content": "/clear",
			})
		}
		m.History = nil
		m.resetTurnState()
		m._suppressEngine = true
		m.ActiveFiles = nil
		m.Reasoning = ""
		m.DiffBlocks = nil
		m.Viewport.SetContent("")
		return m, nil, true
	}

		// Handle /benchmark locally — runs tool call latency benchmark
	if input == "/benchmark" {
		m.History = append(m.History, "> /benchmark")
		m._cachedViewportContent = ""
		m._viewportDirty = true

		results := m.runBenchmark()
		m.History = append(m.History, "_Overview:_\n"+results)

		m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
		m.Viewport.GotoBottom()
		return m, nil, true
	}

	// Mobile-friendly toggle commands
	if input == ":sidebar" || input == ":w" {
		m.ShowSidebar = !m.ShowSidebar
		return m, func() tea.Msg {
			return tea.WindowSizeMsg{Width: m.Width, Height: m.Height}
		}, true
	}

	// Re-enable engine messages for the new request
	m._suppressEngine = false
	if m.SendFunc != nil {
		msgType := "chat"
		if strings.HasPrefix(input, "/") {
			msgType = "command"
		}
		m.SendFunc(map[string]string{
			"type":    msgType,
			"content": input,
		})
	}
	m.History = append(m.History, "> "+input)
	m.resetTurnState()
	m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))
	m.Viewport.GotoBottom()

	return m, nil, true
}

// runBenchmark runs 10 sequential simulated tool calls and returns a formatted report.
func (m Model) runBenchmark() string {
	const iterations = 10

	type measurement struct {
		addTime   time.Duration
		updateTime time.Duration
		renderTime time.Duration
		totalTime  time.Duration
	}

	var measurements [iterations]measurement

	for i := 0; i < iterations; i++ {
		// Use a throwaway telemetry to avoid polluting real tool state
		tel := NewToolTelemetry(50)

		// Phase 1: ToolEvent creation + Add to ring buffer
		t0 := time.Now()
		tel.Add(ToolEvent{
			ID:        fmt.Sprintf("bench_%d", i),
			Name:      "benchmark_tool",
			Args:      fmt.Sprintf("iteration_%d", i+1),
			State:     ToolStateRunning,
			StartTime: time.Now(),
		})
		t1 := time.Now()

		// Phase 2: UpdateLast (state transition)
		tel.UpdateLast(ToolStateSuccess, "OK")
		t2 := time.Now()

		// Phase 3: Render (the telemetry view + viewport content)
		_ = m.RenderToolTelemetry(80)
		_ = m.FullViewportContent(80)
		t3 := time.Now()

		measurements[i] = measurement{
			addTime:    t1.Sub(t0),
			updateTime: t2.Sub(t1),
			renderTime: t3.Sub(t2),
			totalTime:  t3.Sub(t0),
		}
	}

	// Compute totals and averages
	var totalAdd, totalUpdate, totalRender, totalAll time.Duration
	for _, m := range measurements {
		totalAdd += m.addTime
		totalUpdate += m.updateTime
		totalRender += m.renderTime
		totalAll += m.totalTime
	}
	avgAdd := totalAdd / iterations
	avgUpdate := totalUpdate / iterations
	avgRender := totalRender / iterations
	avgAll := totalAll / iterations

	// Find min/max
	minTime := measurements[0].totalTime
	maxTime := measurements[0].totalTime
	for _, m := range measurements[1:] {
		if m.totalTime < minTime {
			minTime = m.totalTime
		}
		if m.totalTime > maxTime {
			maxTime = m.totalTime
		}
	}

	var sb strings.Builder
	sb.WriteString("⚡ Tool Call Latency Benchmark\n\n")
	sb.WriteString(fmt.Sprintf("  Iterations:  %d\n", iterations))
	sb.WriteString(fmt.Sprintf("  Average:     %s\n", avgAll))
	sb.WriteString(fmt.Sprintf("  Min:         %s\n", minTime))
	sb.WriteString(fmt.Sprintf("  Max:         %s\n", maxTime))
	sb.WriteString("\n  Phase breakdown (avg):\n")
	sb.WriteString(fmt.Sprintf("    Create:    %s\n", avgAdd))
	sb.WriteString(fmt.Sprintf("    Update:    %s\n", avgUpdate))
	sb.WriteString(fmt.Sprintf("    Render:    %s\n", avgRender))
	sb.WriteString("\n  Individual iterations:\n")
	for i, m := range measurements {
		sb.WriteString(fmt.Sprintf("    %2d: %s  (create=%s  update=%s  render=%s)\n",
			i+1, m.totalTime, m.addTime, m.updateTime, m.renderTime))
	}

	return sb.String()
}
