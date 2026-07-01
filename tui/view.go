package tui

import (
	"os"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
)

// displayPath replaces the user's home directory prefix with ~ for
// cosmetic display purposes only -- never used for actual file operations.
func displayPath(path string) string {
	home := os.Getenv("HOME")
	if home != "" && strings.HasPrefix(path, home) {
		return "~" + strings.TrimPrefix(path, home)
	}
	return path
}

func (m Model) RenderUsageOverlay() string {
	titleStyle := lipgloss.NewStyle().Foreground(accent).Bold(true).Padding(1, 2)
	labelStyle := lipgloss.NewStyle().Foreground(subtle).Width(22)
	valueStyle := lipgloss.NewStyle().Foreground(fg)
	dimStyle := lipgloss.NewStyle().Foreground(subtle).Padding(1, 2)

	s := m._usageStats
	if s == nil {
		return titleStyle.Render("⚡ Token Usage") + "\n\n" + dimStyle.Render("No data yet.")
	}

	getInt := func(key string) int {
		switch v := s[key].(type) {
		case float64: return int(v)
		case int: return v
		}
		return 0
	}
	getStr := func(key string) string {
		if v, ok := s[key].(string); ok { return v }
		return "—"
	}

	prompt := getInt("prompt_tokens")
	completion := getInt("completion_tokens")
	history := getInt("history_messages")
	compactions := getInt("compaction_events")
	ctxBefore := getInt("context_before")
	ctxAfter := getInt("context_after")
	ctxCurrent := getInt("current_context_est")
	ctxLimit := getInt("context_limit")
	model := getStr("model")
	provider := getStr("provider")

	var sb strings.Builder
	sb.WriteString(titleStyle.Render("⚡ Token Usage") + "\n\n")

	row := func(label, value string) {
		sb.WriteString(labelStyle.Render(label) + valueStyle.Render(value) + "\n")
	}

	row("Model:", fmt.Sprintf("%s @ %s", model, provider))
	sb.WriteString("\n")
	row("Prompt tokens:", fmt.Sprintf("%d", prompt))
	row("Completion tokens:", fmt.Sprintf("%d", completion))
	row("Total tokens:", fmt.Sprintf("%d", prompt+completion))
	sb.WriteString("\n")
	row("History messages:", fmt.Sprintf("%d", history))
	row("Compaction events:", fmt.Sprintf("%d", compactions))

	if ctxBefore > 0 {
		sb.WriteString("\n")
		row("Last compact before:", fmt.Sprintf("%d messages", ctxBefore))
		row("Last compact after:", fmt.Sprintf("%d messages", ctxAfter))
		reduction := 0
		if ctxBefore > 0 {
			reduction = 100 - (ctxAfter * 100 / ctxBefore)
		}
		row("Reduction:", fmt.Sprintf("%d%%", reduction))
	}

	sb.WriteString("\n")
	usagePct := 0
	if ctxLimit > 0 {
		usagePct = ctxCurrent * 100 / ctxLimit
	}
	pctDisplay := fmt.Sprintf("%d%%", usagePct)
	if usagePct > 100 {
		pctDisplay = fmt.Sprintf("%d%% (over limit)", usagePct)
	}
	row("Context estimate:", fmt.Sprintf("%d / %d tokens (%s)", ctxCurrent, ctxLimit, pctDisplay))

	// Progress bar — clamp display to 100% for the bar
	barWidth := 30
	filled := usagePct * barWidth / 100
	if filled > barWidth {
		filled = barWidth
	}
	bar := strings.Repeat("█", filled) + strings.Repeat("░", barWidth-filled)
	barColor := green
	if usagePct > 60 { barColor = yellow }
	if usagePct > 85 { barColor = red }
	sb.WriteString("\n" + lipgloss.NewStyle().Foreground(barColor).Render(bar) + "\n")

	sb.WriteString("\n" + dimStyle.Render("esc or q to close") + "\n")
	return sb.String()
}

func (m Model) RenderModelPicker() string {
	titleStyle := lipgloss.NewStyle().Foreground(accent).Bold(true).Padding(1, 2)
	subtitleStyle := lipgloss.NewStyle().Foreground(subtle).Padding(0, 2)
	itemStyle := lipgloss.NewStyle().Foreground(fg).Padding(0, 2)
	currentStyle := lipgloss.NewStyle().Foreground(green).Bold(true).Padding(0, 2)
	highlightArrow := lipgloss.NewStyle().Foreground(accent).Bold(true)
	dimStyle := lipgloss.NewStyle().Foreground(subtle).Padding(0, 2)
	inputStyle := lipgloss.NewStyle().Foreground(accent).Bold(true)

	var sb strings.Builder
	sb.WriteString(titleStyle.Render("⚡ Select Model") + "\n\n")

	if m._modelPickerCurrent != "" {
		sb.WriteString(dimStyle.Render(fmt.Sprintf("Current: %s", m._modelPickerCurrent)) + "\n\n")
	}

	if m._modelPickerFilter != "" {
		sb.WriteString(inputStyle.Render("Filter: "+m._modelPickerFilter) + dimStyle.Render(fmt.Sprintf("  (%d matches)", len(m._modelPickerItems))) + "\n\n")
	}
	if len(m._modelPickerAllItems) == 0 {
		sb.WriteString(subtitleStyle.Render("Fetching models...") + "\n")
	} else if len(m._modelPickerItems) == 0 {
		sb.WriteString(subtitleStyle.Render("No matches — backspace to clear") + "\n")
	} else {
		for i, model := range m._modelPickerItems {
			// If using arrow navigation (no numeric input buffered), show cursor
			if m._modelPickerInput == "" && i == m._modelPickerIndex {
				sb.WriteString(highlightArrow.Render(fmt.Sprintf(" ▶%2d. %s", i+1, model)) + "\n")
			} else if model == m._modelPickerCurrent {
				sb.WriteString(currentStyle.Render(fmt.Sprintf("    %2d. %s ◄", i+1, model)) + "\n")
			} else {
				sb.WriteString(itemStyle.Render(fmt.Sprintf("    %2d. %s", i+1, model)) + "\n")
			}
		}
	}

	// Dynamic range hint + input buffer display
	total := len(m._modelPickerItems)
	if total > 0 {
		inputDisplay := ""
		if m._modelPickerInput != "" {
			inputDisplay = " [" + inputStyle.Render(m._modelPickerInput) + "]"
		}
		sb.WriteString("\n" + dimStyle.Render(
			fmt.Sprintf("↑↓ to navigate  •  type to filter  •  Enter to select  •  esc to cancel/clear%s", inputDisplay)) + "\n")
	}
	return sb.String()
}

// RenderCommandPicker renders the slash-command autocomplete popup above the textarea.
func (m Model) RenderCommandPicker(width int) string {
	if width < 10 {
		width = 10
	}

	titleStyle := lipgloss.NewStyle().Foreground(accent).Bold(true)
	itemStyle := lipgloss.NewStyle().Foreground(fg)
	selectedStyle := lipgloss.NewStyle().Foreground(green).Bold(true)
	dimStyle := lipgloss.NewStyle().Foreground(subtle)

	const maxVisible = 10
	items := m._cmdPickerItems
	start := 0
	if m._cmdPickerIndex >= maxVisible {
		start = m._cmdPickerIndex - maxVisible + 1
	}
	end := start + maxVisible
	if end > len(items) {
		end = len(items)
	}
	visible := items[start:end]

	var sb strings.Builder
	sb.WriteString(titleStyle.Render("Command") + "\n")
	for i, cmd := range visible {
		idx := start + i
		if idx == m._cmdPickerIndex {
			sb.WriteString(selectedStyle.Render("▶ "+cmd) + "\n")
		} else {
			sb.WriteString(itemStyle.Render("  "+cmd) + "\n")
		}
	}
	if len(items) == 0 {
		sb.WriteString(dimStyle.Render("  No matching commands") + "\n")
	}

	hint := "↑↓ navigate • Enter fill • Esc cancel"
	if len(items) > maxVisible {
		hint = fmt.Sprintf("↑↓ navigate (%d/%d) • Enter fill • Esc cancel", m._cmdPickerIndex+1, len(items))
	}
	sb.WriteString("\n" + dimStyle.Render(hint) + "\n")

	boxStyle := lipgloss.NewStyle().
		Border(lipgloss.RoundedBorder()).
		BorderForeground(accent).
		Padding(0, 1).
		Width(width - 4)

	return boxStyle.Render(sb.String())
}

func (m Model) RenderToolTelemetry(width int) string {
	events := m.Tools.Recent()
	if len(events) == 0 {
		return ""
	}
	
	// Rolling window: only show last 5 tool calls
	const visibleWindow = 5
	if len(events) > visibleWindow {
		events = events[len(events)-visibleWindow:]
	}

	var lines []string
	for _, e := range events {
		icon := toolIconQueued
		color := toolQueued
		switch e.State {
		case ToolStateRunning:
			icon = toolIconRunning
			color = toolRunning
		case ToolStateSuccess:
			icon = toolIconSuccess
			color = toolSuccess
		case ToolStateWarning:
			icon = toolIconWarning
			color = toolWarning
		case ToolStateBlocked:
			icon = toolIconBlocked
			color = toolBlocked
		case ToolStateFailed:
			icon = toolIconFailed
			color = toolFailed
		}

		duration := e.Duration()
		durationStr := ""
		if duration > 0 {
			if duration < time.Second {
				durationStr = fmt.Sprintf("%dms", duration.Milliseconds())
			} else {
				durationStr = fmt.Sprintf("%.1fs", duration.Seconds())
			}
		}

		name := e.Name
		if len(name) > 18 {
			name = name[:17] + "…"
		}

		result := e.Result
		if result == "" && e.State == ToolStateRunning {
			result = "running"
		} else if result == "" {
			result = string(e.State)
		}

		args := e.Args
		if width > 43 {
			if len(args) > width-40 {
				args = args[:width-43] + "…"
			}
		} else {
			args = ""
		}

		line := lipgloss.NewStyle().
			Foreground(color).
			Render(fmt.Sprintf("%s [%s] %s %s %s", icon, durationStr, name, args, result))
		lines = append(lines, line)
	}

	return strings.Join(lines, "\n")
}

func (m Model) View() string {
	viewStart := time.Now()
	defer func() {
		if m._metrics != nil {
			m._metrics.RecordView(time.Since(viewStart))
		}
	}()

	if m._usageOverlayActive {
		return m.RenderUsageOverlay()
	}
	if m._modelPickerActive {
		return m.RenderModelPicker()
	}
	if m._setupActive {
		return m.RenderSetupFlow()
	}

	if m.Width == 0 || m.Height == 0 {
		return "Initializing Kyrex..."
	}

	// --- DRAG MODE: Clean viewport for terminal text selection ---
	if !m.MouseEnabled {
		viewportH := m.Height - 2 // textarea + footer
		if viewportH < 1 {
			viewportH = 1
		}
		vpW := m.Width - 2
		if vpW < 1 {
			vpW = 1
		}
		m.Viewport.Width = vpW
		m.Viewport.Height = viewportH
		m.Textarea.SetWidth(m.Width - 2)
		m.Textarea.SetHeight(1)
		m.Textarea.MaxHeight = 1

		mainContent := viewportStyle.Width(m.Width).MaxWidth(m.Width).Height(viewportH).Render(m.Viewport.View())

		// Confirmation overlay still works in drag mode
		if m.ConfirmID != "" {
			var overlay string
			if m.ConfirmType == "deletion" {
				confirmTitle := lipgloss.NewStyle().Foreground(red).Bold(true).Render("🗑  FILE DELETION PROPOSAL")
				pathLabel := lipgloss.NewStyle().Foreground(accent).Render("Command: " + displayPath(m.ConfirmPath))
				proposalBox := lipgloss.NewStyle().
					Border(lipgloss.RoundedBorder()).
					BorderForeground(red).
					Padding(0, 1).
					Width(m.Width - 6).
					Render(m.ConfirmDiff)
				prompt := lipgloss.NewStyle().Foreground(accent).Bold(true).Render("Proceed with deletion? (y/n)")
				overlay = fmt.Sprintf("%s\n%s\n\n%s\n\n%s", confirmTitle, pathLabel, proposalBox, prompt)
			} else {
				confirmTitle := lipgloss.NewStyle().Foreground(purple).Bold(true).Render("[!] CONFIRM CHANGES")
				pathLabel := lipgloss.NewStyle().Foreground(accent).Render("Proposed Change to: " + displayPath(m.ConfirmPath))
				colWidth := (m.Width / 2) - 4
				leftHeader := lipgloss.NewStyle().Width(colWidth).Foreground(red).Bold(true).Render(" OLD VERSION ")
				rightHeader := lipgloss.NewStyle().Width(colWidth).Foreground(green).Bold(true).Render(" NEW VERSION ")
				diffContent := renderSideBySide(m.ConfirmDiff, colWidth)
				diffBox := lipgloss.NewStyle().
					Border(lipgloss.RoundedBorder()).
					BorderForeground(purple).
					Padding(0, 1).
					Render(fmt.Sprintf("%s  %s\n%s", leftHeader, rightHeader, diffContent))
				prompt := lipgloss.NewStyle().Foreground(accent).Bold(true).Render("Apply this change? (y/n)")
				overlay = fmt.Sprintf("%s\n%s\n\n%s\n\n%s", confirmTitle, pathLabel, diffBox, prompt)
			}
			mainContent = lipgloss.NewStyle().Padding(1, 2).Render(overlay)
		}

		var toast string
		if m.Toast != "" {
			toast = toastStyle.Render(m.Toast)
		}
		if toast != "" {
			return lipgloss.JoinVertical(lipgloss.Left, mainContent, toast, textareaStyle.Width(m.Width).Render(m.Textarea.View()))
		}
		return lipgloss.JoinVertical(lipgloss.Left, mainContent, textareaStyle.Width(m.Width).Render(m.Textarea.View()))
	}

	// --- MOUSE MODE: Full UI ---

	// Use the cached applied layout instead of recalculating from current state.
	// This prevents layout thrashing when textarea content changes but the
	// applied height hasn't caught up yet (due to debounce).
	// The layout is only recalculated in Update() when dimensions actually change.
	layout := m._lastAppliedLayout
	if layout.ViewportWidth == 0 {
		// First render before any Update() — compute initial layout
		layout = m.recalculateLayout()
	}

	showSidebar := layout.ShowSidebar
	sidebarWidth := layout.SidebarWidth
	mainWidth := layout.MainWidth
	viewportHeight := layout.ViewportHeight
	footerHeight := layout.FooterHeight

	// --- Sidebar (cached: doesn't change while typing) ---
	var sb string
	if showSidebar {
		sidebarKey := fmt.Sprintf("%v|%d|%d|%d|%v|%v|%v|%s|%s|%d",
			showSidebar, sidebarWidth, m.Height, footerHeight,
			m.ActiveFiles, m.WorkspaceDirs, m.WorkspaceFiles,
			m.Context, m.SessionBranch, len(m.Timeline.Events))

		if sidebarKey != m._cachedSidebarKey {

			// --- ACTIVE FILES Section ---
			activeHeader := sidebarHeaderStyle.Render("ACTIVE FILES")
			var activeContent string
			if len(m.ActiveFiles) == 0 {
				activeContent = lipgloss.NewStyle().Foreground(subtle).Render("None")
			} else {
				var styledActive []string
				for _, f := range m.ActiveFiles {
					styledActive = append(styledActive, lipgloss.NewStyle().Foreground(fg).Render("- "+pathBasename(f)))
				}
				activeContent = strings.Join(styledActive, "\n")
			}

			// --- WORKSPACE Section ---
			workspaceHeader := sidebarHeaderStyle.Render("WORKSPACE")
			contextStr := lipgloss.NewStyle().Foreground(purple).Render("> " + displayPath(m.Context))

			var workspaceLines []string
			// Directories
			for _, d := range m.WorkspaceDirs {
				workspaceLines = append(workspaceLines, lipgloss.NewStyle().Foreground(fg).Render("📁 "+d+"/"))
			}
			// Key files
			for _, f := range m.WorkspaceFiles {
				workspaceLines = append(workspaceLines, lipgloss.NewStyle().Foreground(fg).Render("📄 "+f))
			}
			workspaceBody := strings.Join(workspaceLines, "\n")
			if workspaceBody == "" {
				workspaceBody = lipgloss.NewStyle().Foreground(subtle).Render("No workspace files")
			}

			// --- EXECUTION TIMELINE Section (only show when there are events) ---
			var timelineSection string
			if len(m.Timeline.Events) > 0 {
				timelineHeader := sidebarHeaderStyle.Render("EXECUTION TIMELINE")
				timelineContent := m.Timeline.Render(sidebarWidth)
				if timelineContent != "" {
					timelineSection = timelineHeader + "\n" + timelineContent
				}
			}

			var sidebarContent string
			if timelineSection != "" {
				sidebarContent = fmt.Sprintf("%s\n%s\n\n%s\n%s\n\n%s\n\n%s",
					activeHeader, activeContent, workspaceHeader, contextStr, workspaceBody, timelineSection)
			} else {
				sidebarContent = fmt.Sprintf("%s\n%s\n\n%s\n%s\n\n%s",
					activeHeader, activeContent, workspaceHeader, contextStr, workspaceBody)
			}
			m._cachedSidebar = sidebarStyle.Copy().Width(sidebarWidth).Height(m.Height - footerHeight).Render(sidebarContent)
			m._cachedSidebarKey = sidebarKey
		}
		sb = m._cachedSidebar
	}

	// --- Main Stack ---

	// Render command picker first so we can reserve its height from the viewport.
	var pickerRendered string
	if m._cmdPickerActive {
		pickerRendered = m.RenderCommandPicker(mainWidth)
	}

	// Tool trace overlay
	var trace string
	if m.CurrentTool != "" {
		safeArgs := m.ToolArgs
		if len(safeArgs) > mainWidth-10 {
			safeArgs = safeArgs[:mainWidth-13] + "..."
		}
		trace = fmt.Sprintf(" [tool] %s(%s)", m.CurrentTool, safeArgs)
		if m.ToolResult != "" {
			resColor := green
			if strings.HasPrefix(m.ToolResult, "ERR") {
				resColor = red
			}
			trace += lipgloss.NewStyle().Foreground(resColor).Render(" -> " + m.ToolResult)
		}
		trace = toolTraceStyle.Width(mainWidth).Render(trace)
	}

	// Adjust viewport render height when the command picker is visible so the
	// overall layout still fits within the terminal. (m is a value receiver, so
	// mutating m.Viewport.Height here does not persist outside View().)
	originalVpHeight := m.Viewport.Height
	if pickerRendered != "" {
		pickerHeight := lipgloss.Height(pickerRendered)
		m.Viewport.Height = viewportHeight - pickerHeight
		if m.Viewport.Height < 1 {
			m.Viewport.Height = 1
		}
	}
	vpContent := m.Viewport.View()

	// Build main stack — avoid extra newlines from empty elements
	vpRendered := viewportStyle.Width(mainWidth).MaxWidth(mainWidth).Height(m.Viewport.Height).Render(vpContent + "\n" + trace)
	m.Viewport.Height = originalVpHeight
	taRendered := textareaStyle.Width(mainWidth).Render(m.Textarea.View())

	mainStack := []string{vpRendered}
	if pickerRendered != "" {
		mainStack = append(mainStack, pickerRendered)
	}
	mainStack = append(mainStack, taRendered)

	var mainContent string
	if !showSidebar {
		contextBar := contextStyle.Width(mainWidth).Render("󰉋 " + m.Context)
		mainStack = append(mainStack, contextBar)
		mainContent = lipgloss.JoinVertical(lipgloss.Left, mainStack...)
	} else {
		mainContent = lipgloss.JoinVertical(lipgloss.Left, mainStack...)
	}

	// --- Confirmation Overlay ---
	if m.ConfirmID != "" {
		var overlay string
		if m.ConfirmType == "deletion" {
			// ── Deletion: single-box proposal (no side-by-side diff) ──
			confirmTitle := lipgloss.NewStyle().Foreground(red).Bold(true).Render("🗑  FILE DELETION PROPOSAL")
			pathLabel := lipgloss.NewStyle().Foreground(accent).Render("Command: " + displayPath(m.ConfirmPath))

			// Render the proposal text as a simple monospace box
			proposalBox := lipgloss.NewStyle().
				Border(lipgloss.RoundedBorder()).
				BorderForeground(red).
				Padding(0, 1).
				Width(mainWidth - 6).
				Render(m.ConfirmDiff)

			prompt := lipgloss.NewStyle().Foreground(accent).Bold(true).Render("Proceed with deletion? (y/n)")

			overlay = lipgloss.NewStyle().
				Padding(1, 2).
				Render(fmt.Sprintf("%s\n%s\n\n%s\n\n%s", confirmTitle, pathLabel, proposalBox, prompt))
		} else {
			// ── Edit: side-by-side diff view (unchanged) ──
			confirmTitle := lipgloss.NewStyle().Foreground(purple).Bold(true).Render("[!] CONFIRM CHANGES")
			pathLabel := lipgloss.NewStyle().Foreground(accent).Render("Proposed Change to: " + displayPath(m.ConfirmPath))

			colWidth := (mainWidth / 2) - 4
			leftHeader := lipgloss.NewStyle().Width(colWidth).Foreground(red).Bold(true).Render(" OLD VERSION ")
			rightHeader := lipgloss.NewStyle().Width(colWidth).Foreground(green).Bold(true).Render(" NEW VERSION ")

			diffContent := renderSideBySide(m.ConfirmDiff, colWidth)

			diffBox := lipgloss.NewStyle().
				Border(lipgloss.RoundedBorder()).
				BorderForeground(purple).
				Padding(0, 1).
				Render(fmt.Sprintf("%s  %s\n%s", leftHeader, rightHeader, diffContent))

			prompt := lipgloss.NewStyle().Foreground(accent).Bold(true).Render("Apply this change? (y/n)")

			overlay = lipgloss.NewStyle().
				Padding(1, 2).
				Render(fmt.Sprintf("%s\n%s\n\n%s\n\n%s", confirmTitle, pathLabel, diffBox, prompt))
		}
		mainContent = overlay
	}

	// --- Footer (cached: doesn't change while typing) ---
	var toast string
	if m.Toast != "" {
		toast = toastStyle.Render(m.Toast)
	}

	footerKey := fmt.Sprintf("%s|%s|%d|%d|%v|%v|%d|%s", m.Phase, m.LLMInfo, m.Width, m.Height, m.IsThinking, m._timerActive, m.Timer, m.Toast)
	var footer string
	if footerKey != m._cachedFooterKey {
		phase := ""
		if m.Phase != PhaseIdle {
			phase = phaseStyle.Render("⚡ " + string(m.Phase))
		}
		brand := brandStyle.Render("KYREX")

		sending := ""
		if m.IsSending {
			dots := strings.Repeat(".", m._sendingTick+1)
			sending = timerStyle.Render(fmt.Sprintf("Sending%s", dots))
		}

		timerDisplay := ""
		if m._timerActive {
			if m.IsThinking {
				dots := strings.Repeat(".", (m.Timer%3)+1)
				timerDisplay = timerStyle.Render(fmt.Sprintf("(%ds) Thinking%s", m.Timer, dots))
			} else {
				timerDisplay = timerStyle.Render(fmt.Sprintf("(%ds)", m.Timer))
			}
		}

		modelInfo := lipgloss.NewStyle().Foreground(accent).Render("☁  " + m.LLMInfo)
		liveWarning := ""
		if m.Workspace != nil && m.Workspace.Root == m.Workspace.Source {
			liveWarning = lipgloss.NewStyle().Foreground(red).Bold(true).Render(" ⚠ LIVE")
		}
		dims := lipgloss.NewStyle().Foreground(subtle).Render(fmt.Sprintf(" [%dx%d]", m.Width, m.Height))
		footerContent := lipgloss.JoinHorizontal(lipgloss.Left, phase, brand, "  ", modelInfo, liveWarning, dims, " ", sending, timerDisplay)
		m._cachedFooter = footerStyle.Width(m.Width).Render(footerContent)
		m._cachedFooterKey = footerKey
	}
	footer = m._cachedFooter

	// --- Final Assembly ---
	if showSidebar {
		if toast != "" {
			return lipgloss.JoinVertical(lipgloss.Left,
				lipgloss.JoinHorizontal(lipgloss.Top, sb, lipgloss.JoinVertical(lipgloss.Left, mainContent, toast)),
				footer,
			)
		}
		return lipgloss.JoinVertical(lipgloss.Left,
			lipgloss.JoinHorizontal(lipgloss.Top, sb, mainContent),
			footer,
		)
	}
	if toast != "" {
		return lipgloss.JoinVertical(lipgloss.Left, mainContent, toast, footer)
	}
	return lipgloss.JoinVertical(lipgloss.Left, mainContent, footer)
}

// @@ hunk header regex: @@ -oldStart[,oldCount] +newStart[,newCount] @@
var hunkHeaderRe = regexp.MustCompile(`@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@`)

func renderSideBySide(diff string, width int) string {
	lines := strings.Split(diff, "\n")
	var left, right []string

	redStyle := lipgloss.NewStyle().Foreground(red)
	greenStyle := lipgloss.NewStyle().Foreground(green)
	dimStyle := lipgloss.NewStyle().Foreground(subtle)
	numStyle := lipgloss.NewStyle().Foreground(subtle)

	gutter := func(n int) string {
		if n == 0 {
			return "      " // blank gutter for extra spacing
		}
		return numStyle.Render(fmt.Sprintf("%4d ", n))
	}

	var oldLineNum, newLineNum int

	for i := 0; i < len(lines); i++ {
		line := lines[i]
		if len(line) == 0 {
			continue
		}

		if strings.HasPrefix(line, "---") || strings.HasPrefix(line, "+++") {
			header := dimStyle.Width(width*2 + 2).Render(truncate(line, width*2))
			left = append(left, header)
			right = append(right, "")
			continue
		}

		if strings.HasPrefix(line, "@@") {
			// Parse hunk header to reset line counters
			matches := hunkHeaderRe.FindStringSubmatch(line)
			if len(matches) >= 5 {
				oldStart, _ := strconv.Atoi(matches[1])
				newStart, _ := strconv.Atoi(matches[3])
				oldLineNum = oldStart
				newLineNum = newStart
			} else {
				oldLineNum = 0
				newLineNum = 0
			}
			header := dimStyle.Width(width*2 + 2).Render(truncate(line, width*2))
			left = append(left, header)
			right = append(right, "")
			continue
		}

		if strings.HasPrefix(line, "-") && i+1 < len(lines) && strings.HasPrefix(lines[i+1], "+") {
			// Changed line — present on both sides
			leftContent := gutter(oldLineNum) + line[1:]
			rightContent := gutter(newLineNum) + lines[i+1][1:]
			left = append(left, redStyle.Width(width).Render(truncate(leftContent, width)))
			right = append(right, greenStyle.Width(width).Render(truncate(rightContent, width)))
			oldLineNum++
			newLineNum++
			i++
		} else if strings.HasPrefix(line, "-") {
			// Pure deletion — only on left (old) side
			leftContent := gutter(oldLineNum) + line[1:]
			left = append(left, redStyle.Width(width).Render(truncate(leftContent, width)))
			right = append(right, strings.Repeat(" ", width))
			oldLineNum++
		} else if strings.HasPrefix(line, "+") {
			// Pure addition — only on right (new) side
			rightContent := gutter(newLineNum) + line[1:]
			left = append(left, strings.Repeat(" ", width))
			right = append(right, greenStyle.Width(width).Render(truncate(rightContent, width)))
			newLineNum++
		} else {
			// Context line (starts with space) — present on both sides
			l := line[1:] // skip unified-diff space prefix
			leftContent := gutter(oldLineNum) + l
			rightContent := gutter(newLineNum) + l
			left = append(left, truncate(leftContent, width))
			right = append(right, truncate(rightContent, width))
			oldLineNum++
			newLineNum++
		}
	}

	// Join lines manually because JoinHorizontal might mismatch line counts if headers were handled poorly
	var result []string
	for i := 0; i < len(left); i++ {
		if right[i] == "" && len(left[i]) > width { // Header case
			result = append(result, left[i])
		} else {
			result = append(result, fmt.Sprintf("%s │ %s", left[i], right[i]))
		}
	}
	return strings.Join(result, "\n")
}

func (m Model) RenderSetupFlow() string {
	titleStyle := lipgloss.NewStyle().Foreground(accent).Bold(true).Padding(1, 2)
	subtitleStyle := lipgloss.NewStyle().Foreground(subtle).Padding(0, 2)
	itemStyle := lipgloss.NewStyle().Foreground(fg).Padding(0, 2)
	selectedStyle := lipgloss.NewStyle().Foreground(green).Bold(true).Padding(0, 2)
	highlightArrow := lipgloss.NewStyle().Foreground(accent).Bold(true)
	dimStyle := lipgloss.NewStyle().Foreground(subtle).Padding(0, 2)
	errorStyle := lipgloss.NewStyle().Foreground(red).Padding(0, 2)
	successStyle := lipgloss.NewStyle().Foreground(green).Padding(0, 2)

	var sb strings.Builder

	sb.WriteString(titleStyle.Render("⚡ Setup Wizard") + "\n\n")

	switch m._setupStep {
	case 0: // Provider picker
		sb.WriteString(subtitleStyle.Render("Step 1: Provider") + "\n")
		sb.WriteString(dimStyle.Render("Choose the AI service Kyrex will use.") + "\n\n")
		providers := []struct {
			num  string
			name string
			url  string
		}{
			{"1", "OpenCode (recommended)", "https://opencode.ai/zen/go/v1"},
			{"2", "OpenRouter", "https://openrouter.ai/api/v1"},
			{"3", "OpenAI", "https://api.openai.com/v1"},
			{"4", "Anthropic", "https://api.anthropic.com"},
			{"5", "Custom", "manual configuration"},
			{"6", "Ollama (local)", "http://localhost:11434/v1"},
		}
		for _, p := range providers {
			if m._setupProvider == p.num {
				sb.WriteString(selectedStyle.Render(fmt.Sprintf(" ▶ %s. %s", p.num, p.name)) + "\n")
			} else {
				sb.WriteString(itemStyle.Render(fmt.Sprintf("    %s. %s", p.num, p.name)) + "\n")
			}
		}
		sb.WriteString("\n" + dimStyle.Render("Select option (1-6) • esc to cancel") + "\n")

	case 1: // API key input
		sb.WriteString(subtitleStyle.Render("Step 2: Authentication") + "\n")
		sb.WriteString(dimStyle.Render("Enter your API key or environment variable name.") + "\n")
		sb.WriteString(dimStyle.Render("Env vars should be ALL_CAPS (e.g. MY_API_KEY)") + "\n\n")
		if m._setupProvider != "" {
			sb.WriteString(dimStyle.Render(fmt.Sprintf("Provider: %s", m._setupProvider)) + "\n")
		}
		if m._setupBaseURL != "" {
			sb.WriteString(dimStyle.Render(fmt.Sprintf("Base URL: %s", m._setupBaseURL)) + "\n")
		}
		maskedInput := m._setupInput
		if len(maskedInput) > 0 && !strings.ContainsAny(maskedInput, "ABCDEFGHIJKLMNOPQRSTUVWXYZ") {
			if len(maskedInput) > 8 {
				maskedInput = maskedInput[:4] + "****" + maskedInput[len(maskedInput)-4:]
			}
		}
		sb.WriteString("\n" + itemStyle.Render(fmt.Sprintf("API Key: %s", maskedInput)) + "\n\n")
		sb.WriteString(dimStyle.Render("Type your key • Enter to continue • esc to cancel") + "\n")

	case 2: // Model picker
		sb.WriteString(subtitleStyle.Render("Step 3: Model") + "\n")
		
		if m._setupCustomModel {
			// Custom model input mode
			sb.WriteString(dimStyle.Render("Enter custom model name:") + "\n\n")
			sb.WriteString(itemStyle.Render(fmt.Sprintf("Model: %s", m._setupInput)) + "\n\n")
			sb.WriteString(dimStyle.Render("Type model name • Enter to confirm • esc to cancel") + "\n")
		} else {
			sb.WriteString(dimStyle.Render("Select which model to use for conversations.") + "\n\n")
			
			// Show filter input
			if m._setupModelFilter != "" {
				sb.WriteString(dimStyle.Render(fmt.Sprintf("Filter: %s_", m._setupModelFilter)) + "\n\n")
			}
			
			if len(m._setupModels) == 0 {
				sb.WriteString(subtitleStyle.Render("Fetching models...") + "\n")
			} else {
				// Use filtered models if available
				modelsToShow := m._setupModels
				if len(m._setupFilteredModels) > 0 {
					modelsToShow = m._setupFilteredModels
				}
				
				// Show scroll position indicator
				totalModels := len(modelsToShow)
				currentPos := m._setupCursorPos + 1
				if currentPos > totalModels {
					currentPos = totalModels
				}
				
				// Show models
				for i, model := range modelsToShow {
					if i == m._setupCursorPos {
						sb.WriteString(highlightArrow.Render(fmt.Sprintf(" ▶ %s", model)) + "\n")
					} else {
						sb.WriteString(itemStyle.Render(fmt.Sprintf("    %s", model)) + "\n")
					}
				}
				
				// Show position indicator
				if totalModels > 0 {
					sb.WriteString(dimStyle.Render(fmt.Sprintf("\nPosition: %d/%d", currentPos, totalModels)) + "\n")
				}
			}
			
			sb.WriteString("\n" + dimStyle.Render("Type to filter • ↑↓ navigate • Enter select • Tab custom • esc cancel") + "\n")
		}

	case 3: // Connection test
		sb.WriteString(subtitleStyle.Render("Step 4: Connection Test") + "\n")
		sb.WriteString(dimStyle.Render("Kyrex will attempt a test request to verify your configuration.") + "\n\n")
		if m._setupTestResult == "" {
			sb.WriteString(subtitleStyle.Render("Press Enter to run connection test") + "\n\n")
		} else if m._setupTestPassed {
			sb.WriteString(successStyle.Render(fmt.Sprintf("✓ CONNECTION PASSED: %s", m._setupTestResult)) + "\n\n")
		} else {
			sb.WriteString(errorStyle.Render(fmt.Sprintf("✗ CONNECTION FAILED: %s", m._setupTestResult)) + "\n\n")
		}
		sb.WriteString(dimStyle.Render("Enter/t to test • s to skip • esc to cancel") + "\n")

	case 4: // Save confirmation
		sb.WriteString(subtitleStyle.Render("Step 5: Review & Save") + "\n\n")
		sb.WriteString(dimStyle.Render("Summary of your configuration:") + "\n")
		sb.WriteString(dimStyle.Render("-----------------------------------------") + "\n")
		sb.WriteString(fmt.Sprintf("Provider:     %s\n", m._setupProvider))
		sb.WriteString(fmt.Sprintf("Model:        %s\n", m._setupModel))
		sb.WriteString(fmt.Sprintf("Base URL:     %s\n", m._setupBaseURL))
		if m._setupAPIKeyEnv != "" {
			sb.WriteString(fmt.Sprintf("API Key:      $%s\n", m._setupAPIKeyEnv))
		} else if m._setupAPIKey != "" {
			masked := m._setupAPIKey
			if len(masked) > 16 {
				masked = masked[:12] + "..."
			}
			sb.WriteString(fmt.Sprintf("API Key:      %s\n", masked))
		}
		if m._setupOllama {
			sb.WriteString(dimStyle.Render("Connection:   - SKIPPED") + "\n")
		} else if m._setupTestPassed {
			sb.WriteString(successStyle.Render("Connection:   ✓ PASS") + "\n")
		} else {
			sb.WriteString(errorStyle.Render("Connection:   ✗ FAIL") + "\n")
		}
		sb.WriteString(dimStyle.Render("-----------------------------------------") + "\n\n")
		sb.WriteString(dimStyle.Render("Save this configuration? (y/n) • esc to cancel") + "\n")
	}

	if m._setupError != "" {
		sb.WriteString("\n" + errorStyle.Render(m._setupError) + "\n")
	}

	return sb.String()
}

func truncate(s string, w int) string {
	if len(s) <= w {
		return s + strings.Repeat(" ", w-len(s))
	}
	return s[:w-1] + "…"
}
