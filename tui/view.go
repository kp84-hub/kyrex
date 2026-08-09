package tui

import (
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
)

// kyrexShape is the raw ASCII block logo used on the full-screen splash.
const kyrexShape = `
██   ██ ██    ██ ██████  ███████ ██   ██
██  ██   ██  ██  ██   ██ ██       ██ ██
█████     ████   ██████  █████     ███
██  ██     ██    ██   ██ ██       ██ ██
██   ██    ██    ██   ██ ███████ ██   ██ `

// displayPath replaces the user's home directory prefix with ~ for
// cosmetic display purposes only -- never used for actual file operations.
func displayPath(path string) string {
	home := os.Getenv("HOME")
	if home != "" && strings.HasPrefix(path, home) {
		return "~" + strings.TrimPrefix(path, home)
	}
	return path
}

// parseCost extracts a numeric dollar value from strings like "≈$0.0012"
// or "$0.02" so the sidebar can compute a per-1K-token rate.
func parseCost(cost string) float64 {
	cost = strings.TrimSpace(cost)
	cost = strings.TrimPrefix(cost, "≈")
	cost = strings.TrimPrefix(cost, "$")
	if v, err := strconv.ParseFloat(cost, 64); err == nil {
		return v
	}
	return 0
}

// formatWithCommas formats an integer with thousands separators, e.g. 7500 -> "7,500".
func formatWithCommas(n int) string {
	if n < 0 {
		return "-" + formatWithCommas(-n)
	}
	s := strconv.Itoa(n)
	var parts []string
	for len(s) > 3 {
		parts = append([]string{s[len(s)-3:]}, parts...)
		s = s[:len(s)-3]
	}
	parts = append([]string{s}, parts...)
	return strings.Join(parts, ",")
}

// normalFooterHint keeps the active-chat controls visible without allowing
// the footer to crowd out status information on narrow terminals.
func normalFooterHint(width int) string {
	switch {
	case width < 60:
		return "Ctrl+B sidebar • / commands"
	case width < 90:
		return "Ctrl+B sidebar • Ctrl+Y copy • / commands"
	default:
		return "Ctrl+B sidebar • Ctrl+Y copy • Esc interrupt • / commands"
	}
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
		case float64:
			return int(v)
		case int:
			return v
		}
		return 0
	}
	getStr := func(key string) string {
		if v, ok := s[key].(string); ok {
			return v
		}
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
	row("Cost:", getStr("cost"))
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
	if usagePct > 60 {
		barColor = yellow
	}
	if usagePct > 85 {
		barColor = red
	}
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

func (m Model) RenderMCPPicker() string {
	titleStyle := lipgloss.NewStyle().Foreground(accent).Bold(true).Padding(1, 2)
	itemStyle := lipgloss.NewStyle().Foreground(fg).Padding(0, 2)
	highlightStyle := lipgloss.NewStyle().Foreground(accent).Bold(true).Padding(0, 2)
	dimStyle := lipgloss.NewStyle().Foreground(subtle).Padding(0, 2)
	inputStyle := lipgloss.NewStyle().Foreground(accent).Bold(true)

	var sb strings.Builder
	sb.WriteString(titleStyle.Render("⚡ MCP Connectors") + "\n\n")
	if m._mcpPickerFilter != "" {
		sb.WriteString(inputStyle.Render("Filter: "+m._mcpPickerFilter) + dimStyle.Render(fmt.Sprintf("  (%d matches)", len(m._mcpPickerItems))) + "\n\n")
	}
	if len(m._mcpPickerAllItems) == 0 {
		sb.WriteString(dimStyle.Render("No connector records received") + "\n")
	} else if len(m._mcpPickerItems) == 0 {
		sb.WriteString(dimStyle.Render("No matches — backspace to clear") + "\n")
	} else {
		for i, connector := range m._mcpPickerItems {
			style := itemStyle
			prefix := "    "
			if m._mcpPickerInput == "" && i == m._mcpPickerIndex {
				style = highlightStyle
				prefix = " ▶  "
			}
			family := connector.Command
			if family == "" {
				family = "local executable"
			}
			auth := connector.Auth.Mode
			if auth == "" {
				auth = "none"
			}
			status := connector.Verification.Status
			if status == "" {
				status = "unverified"
			}
			sb.WriteString(style.Render(fmt.Sprintf("%s%s — %s [%s] auth: %s · %s", prefix, connector.Name, connector.Description, family, auth, status)) + "\n")
		}
	}

	if len(m._mcpPickerItems) > 0 {
		sb.WriteString("\n" + dimStyle.Render("↑↓ to navigate  •  type to filter  •  Enter reserved  •  esc to cancel") + "\n")
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

// splashInputWidth returns the width of the centered input box on the
// full-screen splash for a given terminal width. Capped so the input doesn't
// sprawl across large terminals; floored so it stays usable on tiny ones.
// Single source of truth shared by RenderFullScreenSplash (render path) and
// applyLayout (update path) — the textarea's real width must match its
// rendered width or the internal viewport scroll offset (computed by
// textarea.Update from the real width) won't track wrapped lines, and text
// past the first visible row becomes invisible while typing.
func splashInputWidth(termWidth int) int {
	w := termWidth
	if w > 78 {
		w = 68
	}
	if w < 10 {
		w = 10
	}
	return w
}

// RenderFullScreenSplash produces a full-terminal landing page that covers the entire
// screen — no sidebar, no viewport, no footer chrome. A two-tone block wordmark
// sits directly above a bordered input box that contains the real, functional
// textarea. Model/session metadata are rendered as a single subtle subtitle line.
// Only a real typed chat message (no leading /) exits the splash — slash commands
// (model switching, race/consult setup, /new, etc.) do not. See HasSentFirstMessage.
func (m *Model) RenderFullScreenSplash() string {
	width := m.Width
	height := m.Height
	if width < 1 {
		width = 1
	}
	if height < 1 {
		height = 1
	}

	// Block ASCII wordmark: vibrant 256-color teal, centered on the splash.
	wordmarkBlock := lipgloss.NewStyle().
		Foreground(teal).
		Bold(true).
		Padding(3, 0, 2, 0).
		Render(kyrexShape)

	// Supporting metadata line (model, session, auto-approve) — small and gray.
	// While the model name is still loading, render nothing so the line has zero
	// width and cannot shift horizontally when the real model name arrives.
	modelName := m.Sidebar.CurrentModel
	if modelName == "" || modelName == "unknown" {
		modelName = strings.TrimPrefix(m.LLMInfo, "Model: ")
	}
	session := m.SessionBranch
	if session == "" {
		session = "default"
	}
	autoNote := "auto-approve: off"
	if m.AutoApprove {
		autoNote = "auto-approve: on"
	}

	var metaLine string
	if modelName != "" && modelName != "unknown" {
		metaLine = lipgloss.NewStyle().
			Foreground(darkgrey).
			Render("Model: " + modelName + "  •  Session: " + session + "  •  " + autoNote)
	}

	// Real, functional textarea rendered inside a bordered box directly beneath
	// the wordmark and metadata. Width comes from splashInputWidth — the same
	// value applyLayout pushes into the real component — so the wrap width here
	// matches the wrap width textarea.Update scrolled for.
	inputWidth := splashInputWidth(width)
	if inputWidth > 2 {
		m.Textarea.SetWidth(inputWidth - 2)
	}
	taRendered := textareaFocusedStyle.
		Width(inputWidth).
		Render(m.Textarea.View())

	// Command picker (when active) sits right above the input box.
	var pickerRendered string
	if m._cmdPickerActive {
		pickerRendered = m.RenderCommandPicker(inputWidth)
	}

	// Compact, centered landing block: wordmark → metadata → input box → hints.
	var block []string
	block = append(block, wordmarkBlock, metaLine, "")
	if pickerRendered != "" {
		block = append(block, pickerRendered)
	}
	block = append(block, taRendered)

	// Dim hotkey hints beneath the input box — real commands/keybindings only.
	hintLine := lipgloss.NewStyle().
		Foreground(darkgrey).
		Render("Type / for commands — /setup /model /race /consult")
	block = append(block, "", hintLine)

	contentBlock := lipgloss.JoinVertical(lipgloss.Center, block...)

	// Center the whole unit in the terminal.
	return lipgloss.Place(width, height, lipgloss.Center, lipgloss.Center, contentBlock)
}

// RenderSplashScreen produces a compact welcome block that is vertically centered
// in the main content area by View() using lipgloss.Place. No viewport border or
// separator is rendered — just the wordmark, status pill, model info, and prompt
// line. Once the first turn is sent, the splash is replaced by the normal viewport.
func (m Model) RenderSplashScreen(width int) string {
	if width < 1 {
		width = 1
	}

	// Single-line styled wordmark — no multi-line figlet/ASCII art that could clip
	wordmark := lipgloss.NewStyle().
		Foreground(accent).
		Bold(true).
		Render("K Y R E X")

	// Engine status pill
	var statusPill string
	if m.Sidebar.EngineStatus == "online" {
		statusPill = pillOnline.Render("Engine online")
	} else {
		statusPill = pillOffline.Render(m.Sidebar.EngineStatus)
	}

	// Active model name
	modelName := m.Sidebar.CurrentModel
	if modelName == "" || modelName == "unknown" {
		modelName = strings.TrimPrefix(m.LLMInfo, "Model: ")
	}
	var modelLine string
	if modelName != "" && modelName != "unknown" {
		modelLine = lipgloss.NewStyle().Foreground(darkgrey).Render("Model: " + modelName)
	}

	// Session name
	session := m.SessionBranch
	if session == "" {
		session = "default"
	}
	sessionLine := lipgloss.NewStyle().Foreground(darkgrey).Render("Session: " + session)

	// Prompt line
	promptLine := lipgloss.NewStyle().Foreground(accent).Bold(true).Render("Shall we begin")

	// Compact vertical layout — content is ~8 lines tall, not stretched
	return lipgloss.JoinVertical(lipgloss.Center,
		wordmark,
		"",
		statusPill,
		"",
		modelLine,
		sessionLine,
		"",
		promptLine,
	)
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
	if m._mcpPickerActive {
		return m.RenderMCPPicker()
	}
	if m._setupActive {
		return m.RenderSetupFlow()
	}

	// ── Consult Mode: render helper lane panes ──
	if m._consultActive || m._consultModelPickerActive {
		var consultContent string
		if m._consultModelPickerActive {
			consultContent = m.RenderConsultModelPicker(m.Width)
		} else if m._consult != nil {
			consultContent = m.RenderConsultView()
		} else {
			consultContent = "Initializing consult..."
		}
		taRendered := textareaFocusedStyle.Width(m.Width).Render(m.Textarea.View())
		footer := footerStyle.Width(m.Width).Render(" CONSULT • helpers working • q=cancel consult")
		return lipgloss.JoinVertical(lipgloss.Left, consultContent, taRendered, footer)
	}

	// ── Race Mode: render lane panes instead of normal chat transcript ──
	if m.RaceMode {
		raceContent := m.RenderRaceView()
		taRendered := textareaFocusedStyle.Width(m.Width).Render(m.Textarea.View())
		var footerText string
		if m._raceComparing {
			footerText = " Race comparison • [1-4] view diff  [g] view gate output  m=merge  d/q=discard  ↑↓=navigate"
		} else {
			footerText = " Race mode • q=abort • x=kill first running lane"
		}
		footer := footerStyle.Width(m.Width).Render(footerText)
		return lipgloss.JoinVertical(lipgloss.Left, raceContent, taRendered, footer)
	}

	if m.Width == 0 || m.Height == 0 {
		return "Initializing Kyrex..."
	}

	// ── Full-screen landing page when no conversation history exists ──
	// Covers the entire terminal (no sidebar, no viewport, no footer chrome).
	// The textarea is embedded in the splash. Once the user sends their first
	// splash only exits after a genuine chat message (not a slash command).
	if !m.HasSentFirstMessage {
		return m.RenderFullScreenSplash()
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

		var dragVpContent string
		var mainContent string
		// Always viewport content (full-screen splash is handled by early return above)
		dragVpContent = m.Viewport.View()
		mainContent = viewportStyle.Width(m.Width).MaxWidth(m.Width).Height(viewportH).Render(dragVpContent)

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
			return lipgloss.JoinVertical(lipgloss.Left, mainContent, toast, textareaFocusedStyle.Width(m.Width).Render(m.Textarea.View()))
		}
		return lipgloss.JoinVertical(lipgloss.Left, mainContent, textareaFocusedStyle.Width(m.Width).Render(m.Textarea.View()))
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
		// Build a value-based key for usage stats so the sidebar re-renders
		// when context/cost/token numbers change, not just when the map pointer changes.
		usageKey := "none"
		if s := m._usageStats; s != nil {
			var ctxCurrent, ctxLimit, promptTokens, completionTokens int
			getInt := func(key string) int {
				switch v := s[key].(type) {
				case float64:
					return int(v)
				case int:
					return v
				}
				return 0
			}
			ctxCurrent = getInt("current_context_est")
			ctxLimit = getInt("context_limit")
			promptTokens = getInt("prompt_tokens")
			completionTokens = getInt("completion_tokens")
			cost, _ := s["cost"].(string)
			usageKey = fmt.Sprintf("%d|%d|%d|%d|%s", ctxCurrent, ctxLimit, promptTokens, completionTokens, cost)
		}

		sidebarKey := fmt.Sprintf("%v|%d|%d|%d|%v|%v|%v|%s|%s|%d|%s|%s",
			showSidebar, sidebarWidth, m.Height, footerHeight,
			m.ActiveFiles, m.WorkspaceDirs, m.WorkspaceFiles,
			m.Context, m.SessionBranch, len(m.Timeline.Events), m.ConfirmID, usageKey)

		if sidebarKey != m._cachedSidebarKey {

			// --- CONTEXT Section (context usage + cost) ---
			contextHeader := sidebarHeaderStyle.Render("CONTEXT")

			var ctxCurrent, ctxLimit int
			var costValue float64
			if s := m._usageStats; s != nil {
				getInt := func(key string) int {
					switch v := s[key].(type) {
					case float64:
						return int(v)
					case int:
						return v
					}
					return 0
				}
				ctxCurrent = getInt("current_context_est")
				ctxLimit = getInt("context_limit")
				if costStr, ok := s["cost"].(string); ok {
					costValue = parseCost(costStr)
				}
			}

			usagePct := 0
			if ctxLimit > 0 {
				usagePct = ctxCurrent * 100 / ctxLimit
			}

			var pctColor lipgloss.Color
			switch {
			case usagePct > 85:
				pctColor = red
			case usagePct > 60:
				pctColor = yellow
			default:
				pctColor = green
			}

			tokensLine := lipgloss.NewStyle().Foreground(fg).Render(
				fmt.Sprintf("%s tokens", formatWithCommas(ctxCurrent)))
			pctLine := lipgloss.NewStyle().Foreground(pctColor).Render(
				fmt.Sprintf("%d%% used", usagePct))
			costLine := lipgloss.NewStyle().Foreground(fg).Render(
				fmt.Sprintf("$%.2f spent", costValue))

			contextBody := strings.Join([]string{tokensLine, pctLine, costLine}, "\n")

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
				// Compute maxRows budget for timeline entries.
				// Rows consumed above the timeline content (including headers, active files,
				// workspace tree, and blank separators between sections).
				activeLines := len(m.ActiveFiles)
				if activeLines == 0 {
					activeLines = 1 // "None"
				}
				workspaceLines := len(m.WorkspaceDirs) + len(m.WorkspaceFiles)
				if workspaceLines == 0 {
					workspaceLines = 1 // "No workspace files"
				}
				// Above-timeline row count:
				//   contextHeader(1) + contextBody(3) + blank(1) + activeHeader(1) + activeContent(L) + blank(1)
				//   + workspaceHeader(1) + contextStr(1) + blank(1) + workspaceBody(L) + blank(1) + timelineHeader(1)
				aboveTimeline := activeLines + workspaceLines + 13
				sidebarHeight := m.Height - footerHeight
				maxTimelineRows := sidebarHeight - aboveTimeline
				if maxTimelineRows < 3 {
					maxTimelineRows = 3
				}
				const timelineCap = 100
				if maxTimelineRows > timelineCap {
					maxTimelineRows = timelineCap
				}

				timelineHeader := sidebarHeaderStyle.Render("EXECUTION TIMELINE")
				timelineContent := m.Timeline.Render(sidebarWidth, maxTimelineRows)
				if timelineContent != "" {
					timelineSection = timelineHeader + "\n" + timelineContent
				}
			}

			var sidebarContent string
			if timelineSection != "" {
				sidebarContent = fmt.Sprintf("%s\n%s\n\n%s\n%s\n\n%s\n%s\n\n%s\n\n%s",
					contextHeader, contextBody, activeHeader, activeContent, workspaceHeader, contextStr, workspaceBody, timelineSection)
			} else {
				sidebarContent = fmt.Sprintf("%s\n%s\n\n%s\n%s\n\n%s\n%s\n\n%s",
					contextHeader, contextBody, activeHeader, activeContent, workspaceHeader, contextStr, workspaceBody)
			}

			// ── Truncate sidebar content to fit the allocated height ──
			// sidebarStyle has Padding(1) = 2 vertical lines. Content area
			// inside the Height()-constrained box is (m.Height - footerHeight - 2) lines.
			// Lipgloss Height() may not reliably clip overflow — truncate explicitly
			// so Height() only ever pads (never clips), preventing sidebar overflow
			// from pushing the main pane layout upward as the timeline fills.
			maxContentLines := m.Height - footerHeight - 2
			if maxContentLines < 1 {
				maxContentLines = 1
			}
			if lines := strings.Split(sidebarContent, "\n"); len(lines) > maxContentLines {
				sidebarContent = strings.Join(lines[:maxContentLines], "\n")
			}

			m._cachedSidebar = sidebarStyle.Copy().Width(sidebarWidth).Height(m.Height - footerHeight).Render(sidebarContent)
			m._cachedSidebarKey = sidebarKey
		}
		sb = m._cachedSidebar
	}

	// --- Main Stack ---

	// Render command picker or race model picker first so we can reserve its height from the viewport.
	var pickerRendered string
	if m._cmdPickerActive {
		pickerRendered = m.RenderCommandPicker(mainWidth)
	} else if m._raceModelPickerActive {
		pickerRendered = m.RenderRaceModelPicker(mainWidth)
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
	// Render splash (no turns) or viewport (conversation active).
	// Splash: compact block, vertically centered via lipgloss.Place — no viewport
	// border or separator.  Viewport: normal scrollable chat transcript.
	// Normal viewport rendering (full-screen splash handled by early return above)
	vpContent := m.Viewport.View()
	vpRendered := viewportBorderStyle.Width(mainWidth).MaxWidth(mainWidth).Height(m.Viewport.Height).Render(vpContent)
	m.Viewport.Height = originalVpHeight

	// Textarea border color: bright accent when focused, dim border when blurred.
	// The textarea is always focused (called Focus() in NewModel), so the focused
	// style is active. The blurred style is defined and ready for future use if the
	// textarea is ever blurred programmatically.
	taStyle := textareaFocusedStyle
	taRendered := taStyle.Width(mainWidth).Render(m.Textarea.View())

	// Conversation view: viewport + separator + textarea
	sep := inputSeparatorStyle.Width(mainWidth).Render(strings.Repeat("─", mainWidth))
	mainStack := []string{vpRendered}
	if pickerRendered != "" {
		mainStack = append(mainStack, pickerRendered)
	}
	mainStack = append(mainStack, sep, taRendered)

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

	footerKey := fmt.Sprintf("%s|%s|%d|%d|%v|%v|%d|%s|%v", m.Phase, m.LLMInfo, m.Width, m.Height, m.IsThinking, m._timerActive, m.Timer, m.Toast, m.AutoApprove)
	var footer string
	if footerKey != m._cachedFooterKey {
		phase := ""
		if m.Phase != PhaseIdle {
			phase = pillPhase.Render("⚡ " + string(m.Phase))
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
		hint := lipgloss.NewStyle().Foreground(subtle).Render("  " + normalFooterHint(m.Width))
		footerContent := lipgloss.JoinHorizontal(lipgloss.Left, phase, brand, "  ", modelInfo, liveWarning, dims, " ", sending, timerDisplay, hint)
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

// RenderRaceModelPicker renders the multi-select model picker for the race wizard.
func (m Model) RenderRaceModelPicker(width int) string {
	if width < 10 {
		width = 10
	}

	titleStyle := lipgloss.NewStyle().Foreground(accent).Bold(true)
	itemStyle := lipgloss.NewStyle().Foreground(fg)
	highlightStyle := lipgloss.NewStyle().Foreground(accent).Bold(true)
	dimStyle := lipgloss.NewStyle().Foreground(subtle)

	items := m._raceModelPickerItems
	if items == nil {
		items = []string{}
	}

	var sb strings.Builder

	// Title line with count
	selCount := len(m._raceModelPickerSelected)
	sb.WriteString(titleStyle.Render(fmt.Sprintf("Race models (%d/4 selected):", selCount)))
	if selCount > 0 {
		sb.WriteString(" ")
		for i, sel := range m._raceModelPickerSelected {
			if i > 0 {
				sb.WriteString(", ")
			}
			sb.WriteString(dimStyle.Render(sel))
		}
	}
	sb.WriteString("\n")

	// Filter line
	placeholder := m._raceModelPickerFilter
	if placeholder == "" {
		placeholder = "type to filter"
	}
	sb.WriteString(dimStyle.Render("Filter: ") + m._raceModelPickerFilter + dimStyle.Render("█") + "\n")

	// Items
	for i, model := range items {
		prefix := "  "
		if i == m._raceModelPickerIndex {
			prefix = "▶ "
		}
		// Check if selected
		isSelected := false
		for _, sel := range m._raceModelPickerSelected {
			if sel == model {
				isSelected = true
				break
			}
		}
		check := ""
		if isSelected {
			check = "✓ "
		}
		if i == m._raceModelPickerIndex {
			sb.WriteString(highlightStyle.Render(prefix+check+model) + "\n")
		} else {
			sb.WriteString(itemStyle.Render(prefix+check+model) + "\n")
		}
	}
	if len(items) == 0 {
		sb.WriteString(dimStyle.Render("  (no matching models)") + "\n")
	}

	// Footer hint
	sb.WriteString("\n" + dimStyle.Render("space=select enter=start esc=cancel"))

	boxStyle := lipgloss.NewStyle().
		Border(lipgloss.RoundedBorder()).
		BorderForeground(accent).
		Padding(0, 1).
		Width(width - 4)

	return boxStyle.Render(sb.String())
}

// @@ hunk header regex: @@ -oldStart[,oldCount] +newStart[,newCount] @@
var hunkHeaderRe = regexp.MustCompile(`@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@`)

func renderSideBySide(diff string, width int) string {
	lines := strings.Split(diff, "\n")
	var left, right []string
	var headers []bool

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
			// Header branch: append to left, right, and headers together.
			header := dimStyle.Width(width*2 + 2).Render(truncate(line, width*2))
			left = append(left, header)
			right = append(right, "")
			headers = append(headers, true)
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
			// Hunk-header branch: append to left, right, and headers together.
			header := dimStyle.Width(width*2 + 2).Render(truncate(line, width*2))
			left = append(left, header)
			right = append(right, "")
			headers = append(headers, true)
			continue
		}

		if strings.HasPrefix(line, "-") && i+1 < len(lines) && strings.HasPrefix(lines[i+1], "+") {
			// Changed line — present on both sides.
			leftContent := gutter(oldLineNum) + line[1:]
			rightContent := gutter(newLineNum) + lines[i+1][1:]
			left = append(left, redStyle.Width(width).Render(truncate(leftContent, width)))
			right = append(right, greenStyle.Width(width).Render(truncate(rightContent, width)))
			headers = append(headers, false)
			oldLineNum++
			newLineNum++
			i++
		} else if strings.HasPrefix(line, "-") {
			// Pure deletion — only on left (old) side.
			leftContent := gutter(oldLineNum) + line[1:]
			left = append(left, redStyle.Width(width).Render(truncate(leftContent, width)))
			right = append(right, strings.Repeat(" ", width))
			headers = append(headers, false)
			oldLineNum++
		} else if strings.HasPrefix(line, "+") {
			// Pure addition — only on right (new) side.
			rightContent := gutter(newLineNum) + line[1:]
			left = append(left, strings.Repeat(" ", width))
			right = append(right, greenStyle.Width(width).Render(truncate(rightContent, width)))
			headers = append(headers, false)
			newLineNum++
		} else {
			// Context line (starts with space) — present on both sides.
			l := line[1:] // skip unified-diff space prefix
			leftContent := gutter(oldLineNum) + l
			rightContent := gutter(newLineNum) + l
			left = append(left, truncate(leftContent, width))
			right = append(right, truncate(rightContent, width))
			headers = append(headers, false)
			oldLineNum++
			newLineNum++
		}
	}

	// Join lines manually, using explicit header tracking rather than the
	// styled string length (which includes ANSI escape bytes).
	var result []string
	for i := 0; i < len(left); i++ {
		if headers[i] {
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
		maskedInput := maskAPIKey(m._setupInput)
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
			sb.WriteString(fmt.Sprintf("API Key:      %s\n", maskAPIKey(m._setupAPIKey)))
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

// maskAPIKey masks an API key for display, or returns an env var name unmasked.
// An input is treated as an env var name (shown unmasked) ONLY if it strictly
// matches [A-Z_][A-Z0-9_]* — uppercase letters, digits, underscores only.
// Everything else is masked to show at most the first few characters plus "****".
func maskAPIKey(input string) string {
	if input == "" {
		return ""
	}
	// Strict env-var check: only [A-Z_][A-Z0-9_]*
	isEnvVar := true
	for i, r := range input {
		if i == 0 && r == '_' {
			continue
		}
		if r >= 'A' && r <= 'Z' {
			continue
		}
		if r >= '0' && r <= '9' && i > 0 {
			continue
		}
		if r == '_' {
			continue
		}
		isEnvVar = false
		break
	}
	if isEnvVar {
		return input // show env var names unmasked
	}
	// Mask: show first few chars + "****"
	switch {
	case len(input) >= 6:
		return input[:4] + "****"
	case len(input) >= 2:
		return input[:2] + "****"
	default:
		return "****"
	}
}

func truncate(s string, w int) string {
	if len(s) <= w {
		return s + strings.Repeat(" ", w-len(s))
	}
	return s[:w-1] + "…"
}
