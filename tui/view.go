package tui

import (
	"fmt"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
)

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

	if len(m._modelPickerItems) == 0 {
		sb.WriteString(subtitleStyle.Render("Fetching models...") + "\n")
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
			fmt.Sprintf("↑↓ to navigate  •  type number (1-%d)  •  Enter to select  •  esc to cancel%s", total, inputDisplay)) + "\n")
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
			confirmTitle := lipgloss.NewStyle().Foreground(purple).Bold(true).Render("[!] CONFIRM CHANGES")
			pathLabel := lipgloss.NewStyle().Foreground(accent).Render("Proposed Change to: " + m.ConfirmPath)
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
			mainContent = lipgloss.NewStyle().Padding(1, 2).
				Render(fmt.Sprintf("%s\n%s\n\n%s\n\n%s", confirmTitle, pathLabel, diffBox, prompt))
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
		mode := m.Mode
		if mode == "" {
			mode = string(m.Phase)
		}
		sidebarKey := fmt.Sprintf("%v|%d|%d|%d|%v|%v|%v|%s|%s|%s|%d",
			showSidebar, sidebarWidth, m.Height, footerHeight,
			m.ActiveFiles, m.WorkspaceDirs, m.WorkspaceFiles,
			m.Context, m.SessionBranch, mode, len(m.Timeline.Events))

		if sidebarKey != m._cachedSidebarKey {
			logo := logoStyle.Render("KYREX")

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
			contextStr := lipgloss.NewStyle().Foreground(purple).Render("> " + m.Context)

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

			// --- SESSION Section ---
			sessionHeader := sidebarHeaderStyle.Render("SESSION")
			sessionLines := []string{}
			sessionLines = append(sessionLines, lipgloss.NewStyle().Foreground(subtle).Render("mode:   "+mode))
			if m.SessionBranch != "" {
				sessionLines = append(sessionLines, lipgloss.NewStyle().Foreground(subtle).Render("branch: "+m.SessionBranch))
			}
			sessionContent := strings.Join(sessionLines, "\n")

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
				sidebarContent = fmt.Sprintf("%s\n\n%s\n%s\n\n%s\n%s\n\n%s\n\n%s\n\n%s\n\n%s",
					logo, activeHeader, activeContent, workspaceHeader, contextStr, workspaceBody, sessionHeader, sessionContent, timelineSection)
			} else {
				sidebarContent = fmt.Sprintf("%s\n\n%s\n%s\n\n%s\n%s\n\n%s\n\n%s\n\n%s",
					logo, activeHeader, activeContent, workspaceHeader, contextStr, workspaceBody, sessionHeader, sessionContent)
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
		confirmTitle := lipgloss.NewStyle().Foreground(purple).Bold(true).Render("[!] CONFIRM CHANGES")
		pathLabel := lipgloss.NewStyle().Foreground(accent).Render("Proposed Change to: " + m.ConfirmPath)
		
		// Side-by-Side Diff Rendering
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
		
		overlay := lipgloss.NewStyle().
			Padding(1, 2).
			Render(fmt.Sprintf("%s\n%s\n\n%s\n\n%s", confirmTitle, pathLabel, diffBox, prompt))
		
		mainContent = overlay
	}

	// --- Footer (cached: doesn't change while typing) ---
	var toast string
	if m.Toast != "" {
		toast = toastStyle.Render(m.Toast)
	}

	footerKey := fmt.Sprintf("%s|%s|%d|%d|%v|%d|%s", m.Phase, m.LLMInfo, m.Width, m.Height, m.IsThinking, m.Timer, m.Toast)
	var footer string
	if footerKey != m._cachedFooterKey {
		phase := ""
		if m.Phase != PhaseIdle {
			phase = phaseStyle.Render("⚡ " + string(m.Phase))
		}
		brand := brandStyle.Render("KYREX")

		thinking := ""
		if m.IsThinking {
			dots := strings.Repeat(".", (m.Timer%3)+1)
			thinking = timerStyle.Render(fmt.Sprintf("(%ds) Thinking%s", m.Timer, dots))
		}

		modelInfo := lipgloss.NewStyle().Foreground(accent).Render("☁  " + m.LLMInfo)
		dims := lipgloss.NewStyle().Foreground(subtle).Render(fmt.Sprintf(" [%dx%d]", m.Width, m.Height))
		footerContent := lipgloss.JoinHorizontal(lipgloss.Left, phase, brand, "  ", modelInfo, dims, " ", thinking)
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

func renderSideBySide(diff string, width int) string {
	lines := strings.Split(diff, "\n")
	var left, right []string
	
	redStyle := lipgloss.NewStyle().Foreground(red)
	greenStyle := lipgloss.NewStyle().Foreground(green)
	dimStyle := lipgloss.NewStyle().Foreground(subtle)

	for i := 0; i < len(lines); i++ {
		line := lines[i]
		if len(line) == 0 { continue }

		if strings.HasPrefix(line, "---") || strings.HasPrefix(line, "+++") || strings.HasPrefix(line, "@@") {
			header := dimStyle.Width(width * 2 + 2).Render(truncate(line, width*2))
			left = append(left, header)
			right = append(right, "") // just to keep index synced if we joined later, but we'll do it differently
			continue
		}

		if strings.HasPrefix(line, "-") && i+1 < len(lines) && strings.HasPrefix(lines[i+1], "+") {
			// Changed line
			left = append(left, redStyle.Width(width).Render(truncate(line[1:], width)))
			right = append(right, greenStyle.Width(width).Render(truncate(lines[i+1][1:], width)))
			i++
		} else if strings.HasPrefix(line, "-") {
			left = append(left, redStyle.Width(width).Render(truncate(line[1:], width)))
			right = append(right, strings.Repeat(" ", width))
		} else if strings.HasPrefix(line, "+") {
			left = append(left, strings.Repeat(" ", width))
			right = append(right, greenStyle.Width(width).Render(truncate(line[1:], width)))
		} else {
			// Unchanged
			l := line
			if len(l) > 0 { l = l[1:] } // skip space prefix of unified diff
			left = append(left, truncate(l, width))
			right = append(right, truncate(l, width))
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

func truncate(s string, w int) string {
	if len(s) <= w {
		return s + strings.Repeat(" ", w-len(s))
	}
	return s[:w-1] + "…"
}
