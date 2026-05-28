package tui

import (
	"fmt"
	"regexp"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
)

var (
	// Terminal-safe foreground colors (no backgrounds — inherit terminal theme)
	fg       = lipgloss.Color("#ffffff")
	accent   = lipgloss.Color("#7aa2f7")
	purple   = lipgloss.Color("#bb9af7")
	green    = lipgloss.Color("#9ece6a")
	red      = lipgloss.Color("#f7768e")
	orange   = lipgloss.Color("#e0af68")
	yellow   = lipgloss.Color("#e5c07b")
	border   = lipgloss.Color("#3d3d5c")
	subtle   = lipgloss.Color("#9aa5ce")
	thinkingC = lipgloss.Color("33")

	// Tool state colors (muted, systems-oriented)
	toolQueued   = subtle
	toolRunning  = accent
	toolSuccess = green
	toolWarning = orange
	toolBlocked = yellow
	toolFailed  = red

	// Tool state icons
	toolIconQueued   = "○"
	toolIconRunning  = "⟳"
	toolIconSuccess  = "✓"
	toolIconWarning  = "⚠"
	toolIconBlocked  = "◌"
	toolIconFailed   = "✗"

	thinkingStyle = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder(), false, false, false, true).
			BorderForeground(lipgloss.Color("33")).
			Padding(0, 1).
			MarginBottom(1).
			Foreground(thinkingC).
			Italic(true)

	separatorStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("244")).
			Bold(true).
			MarginTop(1).
			MarginBottom(1)

	// Styles (no Background() — transparent, inherits terminal theme)
	sidebarStyle = lipgloss.NewStyle().
			Border(lipgloss.NormalBorder(), false, true, false, false).
			BorderForeground(border).
			Padding(1)

	sidebarHeaderStyle = lipgloss.NewStyle().
				Foreground(accent).
				Bold(true).
				MarginBottom(1).
				Underline(true)

	viewportStyle = lipgloss.NewStyle().
			Padding(0, 1)

	textareaStyle = lipgloss.NewStyle().
			Padding(0, 1)

	footerStyle = lipgloss.NewStyle().
			Foreground(fg).
			Height(1).
			Padding(0, 1)

	phaseStyle = lipgloss.NewStyle().
			Foreground(purple).
			Padding(0, 1).
			Bold(true).
			MarginRight(1)

	timerStyle = lipgloss.NewStyle().
			Foreground(subtle).
			Italic(true)

	toolTraceStyle = lipgloss.NewStyle().
			Foreground(subtle).
			Italic(true).
			MarginLeft(2)

	logoStyle = lipgloss.NewStyle().
			Foreground(accent).
			Padding(0, 1).
			Bold(true).
			MarginBottom(1)

	brandStyle = lipgloss.NewStyle().
			Foreground(purple).
			Bold(true).
			MarginLeft(1)

	contextStyle = lipgloss.NewStyle().
			Foreground(subtle).
			Italic(true).
			Padding(0, 1)

	toastStyle = lipgloss.NewStyle().
			Foreground(fg).
			Bold(true).
			Padding(0, 2).
			MarginBottom(1)
	selectionStyle = lipgloss.NewStyle().
			Background(lipgloss.Color("#3d3d5c")).
			Foreground(fg)

	overviewStyle = lipgloss.NewStyle().
			Foreground(fg).
			MarginTop(1)

	missionSummaryStyle = lipgloss.NewStyle().
				Foreground(subtle).
				Padding(1, 1).
				Border(lipgloss.RoundedBorder(), false, false, false, false).
				BorderForeground(border).
				MarginTop(1)

	telemetryStyle = lipgloss.NewStyle().
			Foreground(subtle).
			Padding(0, 1)
)

func (m Model) RenderModelPicker() string {
	titleStyle := lipgloss.NewStyle().Foreground(accent).Bold(true).Padding(1, 2)
	subtitleStyle := lipgloss.NewStyle().Foreground(subtle).Padding(0, 2)
	itemStyle := lipgloss.NewStyle().Foreground(fg).Padding(0, 2)
	currentStyle := lipgloss.NewStyle().Foreground(green).Bold(true).Padding(0, 2)
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
			marker := " "
			style := itemStyle
			if model == m._modelPickerCurrent {
				marker = "◄"
				style = currentStyle
			}
			sb.WriteString(style.Render(fmt.Sprintf("  %2d. %s %s", i+1, model, marker)) + "\n")
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
			fmt.Sprintf("Type a number (1-%d) then Enter  •  esc to cancel%s", total, inputDisplay)) + "\n")
	}
	return sb.String()
}

func (m Model) RenderToolTelemetry(width int) string {
	events := m.Tools.Recent()
	if len(events) == 0 {
		return ""
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
		if len(args) > width-40 {
			args = args[:width-43] + "…"
		}

		line := lipgloss.NewStyle().
			Foreground(color).
			Render(fmt.Sprintf("%s [%s] %s %s %s", icon, durationStr, name, args, result))
		lines = append(lines, line)
	}

	return strings.Join(lines, "\n")
}

func (m Model) RenderExecutionTree(width int) string {
	if m.ExecTree == nil || m.ExecTree.Root == nil {
		return ""
	}

	var lines []string
	indent := "  "

	var renderNode func(node *ExecNode, prefix string, isLast bool)
	renderNode = func(node *ExecNode, prefix string, isLast bool) {
		connector := "├─ "
		if isLast {
			connector = "└─ "
		}

		stateIcon := "○"
		stateColor := subtle
		switch node.State {
		case ExecNodeRunning:
			stateIcon = "⟳"
			stateColor = accent
		case ExecNodeSuccess:
			stateIcon = "✓"
			stateColor = green
		case ExecNodeWarning:
			stateIcon = "⚠"
			stateColor = orange
		case ExecNodeFailed:
			stateIcon = "✗"
			stateColor = red
		case ExecNodeBlocked:
			stateIcon = "◌"
			stateColor = yellow
		}

		nodeStyle := lipgloss.NewStyle().Foreground(stateColor)
		if node.active {
			nodeStyle = nodeStyle.Bold(true)
		}

		line := prefix + connector + nodeStyle.Render(stateIcon+" "+node.Label)
		lines = append(lines, line)

		childPrefix := prefix
		if isLast {
			childPrefix += "   "
		} else {
			childPrefix += "│  "
		}

		for i, child := range node.Children {
			renderNode(child, childPrefix, i == len(node.Children)-1)
		}
	}

	// Render plan and exec roots if they have children
	if len(m.ExecTree.Root.Children) > 0 {
		rootStyle := lipgloss.NewStyle().Foreground(purple).Bold(true)
		lines = append(lines, rootStyle.Render("○ Kyrex"))
		for i, child := range m.ExecTree.Root.Children {
			renderNode(child, indent, i == len(m.ExecTree.Root.Children)-1)
		}
	}

	if len(lines) == 0 {
		return ""
	}
	return strings.Join(lines, "\n")
}

func (m Model) View() string {
	if m._modelPickerActive {
		return m.RenderModelPicker()
	}

	if m.Width == 0 || m.Height == 0 {
		return "Initializing Kyrex..."
	}

	// Update viewport content with selection highlights baked in via absolute indexing
	m.Viewport.SetContent(m.FullViewportContent(m.Viewport.Width))

	// --- DRAG MODE: Clean viewport for terminal text selection ---
	if !m.MouseEnabled {
		viewportH := m.Height - 3 // textarea + footer + status
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

	// Responsive sidebar logic: Hide sidebar if terminal is too narrow
	// PC users can still toggle with Ctrl+B
	showSidebar := m.ShowSidebar
	if m.ConfirmID != "" {
		showSidebar = false
	}

	// Calculate dimensions
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

	footerHeight := 1
	textareaHeight := 1
	statusHeight := 1
	viewportHeight := m.Height - textareaHeight - footerHeight - statusHeight
	if viewportHeight < 1 {
		viewportHeight = 1
	}

	// Configure components
	vpW := mainWidth - 2
	if vpW < 1 {
		vpW = 1
	}
	m.Viewport.Width = vpW
	m.Viewport.Height = viewportHeight
	m.Textarea.SetWidth(mainWidth - 2)
	m.Textarea.SetHeight(1)
	m.Textarea.MaxHeight = 1

	// --- Sidebar ---
	var sb string
	if showSidebar {
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
		mode := m.Mode
		if mode == "" {
			mode = string(m.Phase)
		}
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
		sb = sidebarStyle.Copy().Width(sidebarWidth).Height(m.Height - footerHeight).Render(sidebarContent)
	}

	// --- Main Stack ---
	vpContent := m.Viewport.View()
	
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

	// Build main stack — avoid extra newlines from empty elements
	vpRendered := viewportStyle.Width(mainWidth).MaxWidth(mainWidth).Height(viewportHeight).Render(vpContent + "\n" + trace)
	taRendered := textareaStyle.Width(mainWidth).Render(m.Textarea.View())

	var mainContent string
	if !showSidebar {
		contextBar := contextStyle.Width(mainWidth).Render("󰉋 " + m.Context)
		mainContent = lipgloss.JoinVertical(lipgloss.Left, vpRendered, taRendered, contextBar)
	} else {
		mainContent = lipgloss.JoinVertical(lipgloss.Left, vpRendered, taRendered)
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

	// --- Footer ---
	var toast string
	if m.Toast != "" {
		toast = toastStyle.Render(m.Toast)
	}
	phase := phaseStyle.Render("⚡ " + string(m.Phase))
	brand := brandStyle.Render("KYREX")

	thinking := ""
	if m.IsThinking {
		dots := strings.Repeat(".", (m.Timer%3)+1)
		thinking = timerStyle.Render(fmt.Sprintf("(%ds) Thinking%s", m.Timer, dots))
	}

	modelInfo := lipgloss.NewStyle().Foreground(accent).Render("☁  " + m.LLMInfo)
	dims := lipgloss.NewStyle().Foreground(subtle).Render(fmt.Sprintf(" [%dx%d]", m.Width, m.Height))
	footerContent := lipgloss.JoinHorizontal(lipgloss.Left, phase, brand, "  ", modelInfo, dims, " ", thinking)
	footer := footerStyle.Width(m.Width).Render(footerContent)

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

func (m Model) RenderViewportWithSelection() string {
	content := m.Viewport.View()
	if (!m.Selecting && m.SelectStart == m.SelectEnd) || m.SelectStart.Line < 0 {
		return content
	}

	lines := strings.Split(content, "\n")

	start := m.SelectStart
	end := m.SelectEnd

	// Normalize: start should be before end
	if start.Line > end.Line || (start.Line == end.Line && start.Col > end.Col) {
		start, end = end, start
	}

	// Map absolute line indices to visible viewport-relative indices
	startVisible := start.Line - m.Viewport.YOffset
	endVisible := end.Line - m.Viewport.YOffset

	var result []string
	for visibleY, line := range lines {
		if visibleY < startVisible || visibleY > endVisible {
			result = append(result, line)
			continue
		}

		// This line has selection.
		// Strip ANSI to apply selection highlight cleanly on top.
		cleanLine := stripAnsi(line)
		runes := []rune(cleanLine)

		colStart := 0
		if visibleY == startVisible {
			colStart = start.Col
		}

		colEnd := len(runes)
		if visibleY == endVisible {
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

		before := string(runes[:colStart])
		selected := string(runes[colStart:colEnd])
		after := string(runes[colEnd:])

		highlighted := before + selectionStyle.Render(selected) + after
		result = append(result, highlighted)
	}

	return strings.Join(result, "\n")
}

func stripAnsi(str string) string {
	// Simple ANSI stripper regex
	// In production, use a library like muesli/termenv
	const ansi = "[\u001B\u009B][[()#;?]*(?:[0-9]{4,6})?(?:[0-9]{1,4}(?:;[0-9]{0,4})*)?[0-9A-ORZcf-nqry=><]"
	var re = regexp.MustCompile(ansi)
	return re.ReplaceAllString(str, "")
}

func truncate(s string, w int) string {
	if len(s) <= w {
		return s + strings.Repeat(" ", w-len(s))
	}
	return s[:w-1] + "…"
}
