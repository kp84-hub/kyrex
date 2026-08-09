package tui

import (
	"fmt"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
	"github.com/kp84-hub/kx/tui/components"
	"github.com/mattn/go-runewidth"
)

// GetSelectedText extracts clean text from the viewport selection.
// Uses the SAME content that's rendered to the viewport (FullViewportContent)
// to ensure selection line numbers match what the user actually sees.
// The previous implementation used HistoryContentClean which rendered content
// in a completely different layout, causing line-number mismatches and
// inaccurate clipboard text.
func (m Model) GetSelectedText() string {
	if m.SelectStart == m.SelectEnd {
		return ""
	}

	// Use the cached viewport content (populated during selection drag via
	// FullViewportContent). Fall back to rebuilding if cache is empty.
	view := m._cachedViewportContent
	if view == "" {
		// Rebuild using the same pipeline as FullViewportContent
		historyContent, historyLines := m.HistoryContent(m.Viewport.Width)
		view = historyContent + m.CurrentTurnContent(m.Viewport.Width, historyLines)
		telemetry := m.RenderToolTelemetry(m.Viewport.Width)
		if telemetry != "" {
			view += telemetryStyle.Width(m.Viewport.Width).Render(telemetry) + "\n"
		}
		if m.MissionSummary != "" {
			view += missionSummaryStyle.Width(m.Viewport.Width).Render(m.MissionSummary) + "\n"
		}
	}
	lines := strings.Split(view, "\n")

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
		// Strip ANSI escape codes to get clean text
		cleanLine := stripANSI(lines[lineIdx])

		colStart := 0
		if lineIdx == start.Line {
			colStart = start.Col
		}
		colEnd := runewidth.StringWidth(cleanLine)
		if lineIdx == end.Line {
			colEnd = end.Col
		}

		// Clamp to valid range
		if colStart < 0 {
			colStart = 0
		}
		lineWidth := runewidth.StringWidth(cleanLine)
		if colStart > lineWidth {
			colStart = lineWidth
		}
		if colEnd < 0 {
			colEnd = 0
		}
		if colEnd > lineWidth {
			colEnd = lineWidth
		}
		if colEnd < colStart {
			colEnd = colStart
		}

		result = append(result, extractLineRange(cleanLine, colStart, colEnd))
	}

	return strings.Join(result, "\n")
}

// stripANSI removes ANSI escape sequences from a string.
func stripANSI(s string) string {
	// Match ANSI escape sequences: ESC[...m or ESC[...H etc.
	var result strings.Builder
	inEscape := false
	for _, r := range s {
		if r == '\x1b' {
			inEscape = true
			continue
		}
		if inEscape {
			// End of escape sequence at letters
			if (r >= 'A' && r <= 'Z') || (r >= 'a' && r <= 'z') {
				inEscape = false
			}
			continue
		}
		result.WriteRune(r)
	}
	return result.String()
}

// cellPosToRuneIndex converts a cell position (from mouse X) to a rune index.
// Wide characters (e.g., emoji) occupy 2 cells but are 1 rune.
// Returns -1 if the cell position is beyond the string.
func cellPosToRuneIndex(s string, cellPos int) int {
	runes := []rune(s)
	cell := 0
	for i, r := range runes {
		rw := runewidth.RuneWidth(r)
		if cellPos >= cell && cellPos < cell+rw {
			return i
		}
		cell += rw
		if cell > cellPos {
			// Between characters, return next rune
			return i
		}
	}
	return -1
}

// extractLineRange extracts a substring from a line using cell-position-based columns.
// This correctly handles wide characters (emoji, CJK) where 1 rune ≠ 1 cell.
func extractLineRange(line string, colStart, colEnd int) string {
	runes := []rune(line)
	if len(runes) == 0 {
		return ""
	}

	// Convert cell positions to rune indices
	riStart := cellPosToRuneIndex(line, colStart)
	riEnd := cellPosToRuneIndex(line, colEnd)

	if riStart == -1 {
		riStart = len(runes)
	}
	if riEnd == -1 {
		riEnd = len(runes)
	}

	// Clamp to valid range
	if riStart < 0 {
		riStart = 0
	}
	if riStart > len(runes) {
		riStart = len(runes)
	}
	if riEnd < 0 {
		riEnd = 0
	}
	if riEnd > len(runes) {
		riEnd = len(runes)
	}
	if riEnd < riStart {
		riEnd = riStart
	}

	return string(runes[riStart:riEnd])
}

// HistoryContentClean builds a plain (non-selection-aware) history buffer.
// Used by GetSelectedText for extracting text content.
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
			content += logsStyleClean.Render(inner) + "\n\n"
		} else if strings.HasPrefix(h, "_DiffContent:_") {
			inner := strings.TrimPrefix(h, "_DiffContent:_")
			content += "[Diff]\n" + inner + "\n" + separatorStyle.Render("────────────────────────────────────────────────") + "\n\n"
		} else if strings.HasPrefix(h, "_Overview:_") {
			inner := strings.TrimPrefix(h, "_Overview:_")
			content += "Overview\n" + assistantStyle.Render(inner) + "\n\n"
		} else {
			content += assistantStyle.Render(h) + "\n\n"
		}
	}
	// Add current streaming content
	if m.Reasoning != "" {
		content += "[Thinking]\n" + thinkingStyleClean.Render(m.Reasoning) + "\n\n"
	}
	if m.CurrToken != "" {
		content += assistantStyle.Render(m.CurrToken) + "\n\n"
	}

	return content
}

// HistoryContent builds the history buffer with selection highlights baked in via absolute indexing.
// Returns both the rendered content AND the total line count in a single rendering pass.
// The line count is used by CurrentTurnContent/ReasoningContent for selection offset calculations,
// eliminating the need for a separate countHistoryLines() pass.
func (m Model) HistoryContent(width int) (string, int) {
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

	// Group history items into turns
	type turnGroup struct {
		userMsg     string
		thinking    string
		tools       []string
		diffContent []string
		response    string
		logs        []string
		other       []string
	}

	var turns []turnGroup
	var current *turnGroup

	for _, h := range m.History {
		if strings.HasPrefix(h, "> ") {
			if current != nil {
				turns = append(turns, *current)
			}
			current = &turnGroup{userMsg: h[2:]}
		} else if current != nil {
			if strings.HasPrefix(h, "_Thinking:_") {
				current.thinking = strings.TrimPrefix(h, "_Thinking:_")
			} else if strings.HasPrefix(h, "_Tool:_") {
				current.tools = append(current.tools, strings.TrimPrefix(h, "_Tool:_"))
			} else if strings.HasPrefix(h, "_DiffContent:_") {
				current.diffContent = append(current.diffContent, strings.TrimPrefix(h, "_DiffContent:_"))
			} else if strings.HasPrefix(h, "_Overview:_") {
				current.response = strings.TrimPrefix(h, "_Overview:_")
			} else if strings.HasPrefix(h, "_Progress:_") {
				current.logs = append(current.logs, strings.TrimPrefix(h, "_Progress:_"))
			} else if strings.HasPrefix(h, "_Logs:_") {
				current.logs = append(current.logs, strings.TrimPrefix(h, "_Logs:_"))
			} else {
				current.other = append(current.other, h)
			}
		} else {
			if current != nil {
				turns = append(turns, *current)
			}
			current = &turnGroup{other: []string{h}}
		}
	}
	if current != nil {
		turns = append(turns, *current)
	}

	// Render each turn as a complete visual packet
	for _, turn := range turns {
		if turn.userMsg != "" {
			emit(lipgloss.NewStyle().Foreground(cyanDim).Bold(true).Render("> You"))
			// Truncate user prompts > 15 lines for display
			userLines := strings.Split(turn.userMsg, "\n")
			if len(userLines) > 15 {
				userLines = []string{userLines[0] + fmt.Sprintf(" [+%d lines]", len(userLines)-1)}
			}
			emitBlock(strings.Split(style.Render(strings.Join(userLines, "\n")), "\n"))
			content.WriteString("\n")
			absLine++
		}

		if turn.thinking != "" {
			thoughtContent := lipgloss.NewStyle().Foreground(thinkingC).Italic(true).Render("\U000f024b  Thought") + "\n" + lipgloss.NewStyle().Foreground(darkgrey).Render(turn.thinking)
			thoughtBox := lipgloss.NewStyle().
				Border(lipgloss.RoundedBorder()).
				BorderForeground(thinkingC).
				Padding(0, 1).
				Width(width - 4).
				Render(thoughtContent)
			emitBlock(strings.Split(thoughtBox, "\n"))
			content.WriteString("\n")
			absLine++
		}

		for _, tool := range turn.tools {
			emit(lipgloss.NewStyle().Foreground(orange).Render("⚙ " + tool))
		}
		if len(turn.tools) > 0 {
			content.WriteString("\n")
			absLine++
		}

		for _, diffContent := range turn.diffContent {
			for _, dl := range strings.Split(diffContent, "\n") {
				emit(dl)
			}
			emit(separatorStyle.Width(width).Render("────────────────────────────────────────────────"))
			content.WriteString("\n")
			absLine++
		}

		if turn.response != "" {
			emit(lipgloss.NewStyle().Foreground(green).Bold(true).Render("\U000f012c  Overview"))
			emitBlock(strings.Split(overviewStyle.Width(width).Render(turn.response), "\n"))
			content.WriteString("\n")
			absLine++
		}

		for _, log := range turn.logs {
			emitBlock(strings.Split(lipgloss.NewStyle().Foreground(subtle).Width(width).Render(log), "\n"))
			content.WriteString("\n")
			absLine++
		}

		for _, other := range turn.other {
			kyrexContent := lipgloss.NewStyle().Foreground(purple).Bold(true).Render("KYREX") + "\n" + other
			kyrexBox := lipgloss.NewStyle().
				Border(lipgloss.RoundedBorder()).
				BorderForeground(purple).
				Padding(0, 1).
				Width(width - 4).
				Render(kyrexContent)
			emitBlock(strings.Split(kyrexBox, "\n"))
			content.WriteString("\n")
			absLine++
		}
	}

	return content.String(), absLine
}

// ReasoningContent builds the active reasoning block with selection highlights.
// Accepts historyLineCount from HistoryContent() to avoid redundant rendering pass.
func (m Model) ReasoningContent(width int, historyLineCount int) string {
	if m.Reasoning == "" {
		return ""
	}

	selStart, selEnd := m.SelectStart.Line, m.SelectEnd.Line
	if selStart > selEnd {
		selStart, selEnd = selEnd, selStart
	}

	selecting := m.Selecting
	absLine := historyLineCount

	var content strings.Builder

	emit := func(line string) {
		if selecting && absLine >= selStart && absLine <= selEnd {
			content.WriteString("\x1b[7m" + line + "\x1b[27m\n")
		} else {
			content.WriteString(line + "\n")
		}
		absLine++
	}

	thoughtContent := lipgloss.NewStyle().Foreground(thinkingC).Italic(true).Render("\U000f024b  Thought") + "\n" + lipgloss.NewStyle().Foreground(darkgrey).Render(m.Reasoning)
	thoughtBox := lipgloss.NewStyle().
		Border(lipgloss.RoundedBorder()).
		BorderForeground(thinkingC).
		Padding(0, 1).
		Width(width - 4).
		Render(thoughtContent)
	for _, tl := range strings.Split(thoughtBox, "\n") {
		emit(tl)
	}

	return content.String()
}

// FullViewportContent builds the complete viewport buffer with selection highlights applied.
// Incremental rendering: stable history is cached and only rebuilt when it changes.
// During streaming, only the dynamic tail (reasoning/tokens/telemetry) is re-rendered.
// During PhaseExecute, returns compact view (current step + tool telemetry) to prevent scroll overload.
func (m *Model) FullViewportContent(width int) string {

	fvcStart := time.Now()

	// Check if stable history cache is still valid
	historyLen := len(m.History)
	lastTurnLen := 0
	if historyLen > 0 {
		lastTurnLen = len(m.History[historyLen-1])
	}
	historyContent := ""
	historyLines := 0
	cacheHit := false

	// Invalidate cache during selection so highlights are rendered
	needsRefresh := m.Selecting || (m.SelectStart != m.SelectEnd)

	if m._stableHistoryLen == historyLen &&
		m._stableLastTurnLen == lastTurnLen &&
		m._stableHistoryWidth == width &&
		m._stableHistoryContent != "" &&
		!needsRefresh {
		// Cache hit: reuse stable history
		historyContent = m._stableHistoryContent
		historyLines = m._stableHistoryLines
		cacheHit = true
	} else {
		// Cache miss: re-render history (only happens when turns are added/removed or width changes)
		historyContent, historyLines = m.HistoryContent(width)
		m._stableHistoryContent = historyContent
		m._stableHistoryLines = historyLines
		m._stableHistoryLen = historyLen
		m._stableHistoryWidth = width
		m._stableLastTurnLen = lastTurnLen
	}

	var content strings.Builder

	// 1. Full conversation history (all completed turns) — from cache during streaming
	content.WriteString(historyContent)

	// 2. Current active turn (reasoning + diffs + response) — re-rendered each call
	content.WriteString(m.CurrentTurnContent(width, historyLines))

	// 3. Tool telemetry feed
	telemetry := m.RenderToolTelemetry(width)
	if telemetry != "" {
		content.WriteString(telemetryStyle.Width(width).Render(telemetry) + "\n")
	}

	// 4. Mission summary
	if m.MissionSummary != "" {
		content.WriteString(missionSummaryStyle.Width(width).Render(m.MissionSummary) + "\n")
	}

	result := content.String()

	// Breathing room: always pad the tail with blank lines so GotoBottom()
	// anchors the viewport with margin below the real content instead of
	// pinning the last real line flush against the bottom edge.
	if !strings.HasSuffix(result, "\n") {
		result += "\n"
	}
	result += "\n\n"

	// Record FVC metrics
	if m._metrics != nil {
		m._metrics.RecordFVC(time.Since(fvcStart), cacheHit)
	}

	// Skip SetContent if content hasn't actually changed (avoids viewport recalc)
	if result == m._cachedViewportContent && m._cachedWidth == width {
		if m._metrics != nil {
			m._metrics.RecordSetContent(false)
		}
		return result
	}
	m._cachedViewportContent = result
	m._cachedWidth = width

	if m._metrics != nil {
		m._metrics.RecordSetContent(true)
	}

	return result
}

// CurrentTurnContent renders only the ACTIVE streaming content for the current turn.
// Accepts historyLineCount from HistoryContent() to avoid redundant rendering pass.
func (m Model) CurrentTurnContent(width int, historyLineCount int) string {
	if m.Reasoning == "" && m.CurrToken == "" && len(m.DiffBlocks) == 0 {
		return ""
	}

	selStart, selEnd := m.SelectStart.Line, m.SelectEnd.Line
	if selStart > selEnd {
		selStart, selEnd = selEnd, selStart
	}

	var content strings.Builder
	absLine := historyLineCount
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

	// 1. Active reasoning
	if m.Reasoning != "" {
		thoughtContent := lipgloss.NewStyle().Foreground(thinkingC).Italic(true).Render("\U000f024b  Thought") + "\n" + lipgloss.NewStyle().Foreground(darkgrey).Render(m.Reasoning)
		thoughtBox := lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(thinkingC).
			Padding(0, 1).
			Width(width - 4).
			Render(thoughtContent)
		for _, tl := range strings.Split(thoughtBox, "\n") {
			emit(tl)
		}
		content.WriteString("\n")
		absLine++
	}

	// 2. Side-by-Side Diff Blocks
	if len(m.DiffBlocks) > 0 {
		diffContent := components.RenderSideBySideStream(m.DiffBlocks, width)
		if diffContent != "" {
			for _, dl := range strings.Split(diffContent, "\n") {
				emit(dl)
			}
			emit(separatorStyle.Width(width).Render("────────────────────────────────────────────────"))
			content.WriteString("\n")
			absLine++
		}
	}

	// 3. Active streaming tokens
	if m.CurrToken != "" {
		kyrexContent := lipgloss.NewStyle().Foreground(purple).Bold(true).Render("KYREX") + "\n" + m.CurrToken
		kyrexBox := lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(purple).
			Padding(0, 1).
			Width(width - 4).
			Render(kyrexContent)
		emitBlock(strings.Split(kyrexBox, "\n"))
	}

	return content.String()
}

// generateMissionSummary creates the completion summary after a turn with tool calls.
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

// humanReadableTitle generates a short display title for tool calls.
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
			// Strip redundant "cd /path && " prefix — working dir is already in sidebar
			cmdName := c
			if idx := strings.Index(cmdName, "&& "); idx != -1 {
				prefix := cmdName[:idx]
				if strings.HasPrefix(strings.TrimSpace(prefix), "cd ") {
					cmdName = strings.TrimSpace(cmdName[idx+3:])
				}
			}
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

// pathBasename extracts the filename from a path.
func pathBasename(path string) string {
	parts := strings.Split(path, "/")
	for i := len(parts) - 1; i >= 0; i-- {
		if parts[i] != "" {
			return parts[i]
		}
	}
	return path
}

// extractCurrentStep parses the token stream to find the current step header.
// Returns the last "##" header + next 1-2 lines (compact view during execution).
func extractCurrentStep(token string) string {
	lines := strings.Split(token, "\n")

	// Find last "##" header
	var lastHeaderIdx = -1
	for i, line := range lines {
		if strings.HasPrefix(line, "##") {
			lastHeaderIdx = i
		}
	}

	if lastHeaderIdx == -1 {
		// No header found — return last 3 lines
		start := len(lines) - 3
		if start < 0 {
			start = 0
		}
		return strings.Join(lines[start:], "\n")
	}

	// Return header + next 2 lines
	end := lastHeaderIdx + 3
	if end > len(lines) {
		end = len(lines)
	}

	return strings.Join(lines[lastHeaderIdx:end], "\n")
}

// CompactViewportContent builds a compact view for active execution.
// Shows: current step (from extractCurrentStep) + tool telemetry.
// This prevents scroll overload during multi-step tasks.
func (m *Model) CompactViewportContent(width int) string {
	var content strings.Builder

	// 1. Render completed history (stable — from cache)
	historyContent, _ := m.HistoryContent(width)
	content.WriteString(historyContent)

	// 2. Current step (compact — only last "##" header + 1-2 lines)
	if m.CurrToken != "" {
		currentStep := extractCurrentStep(m.CurrToken)
		if currentStep != "" {
			// Render with purple "KYREX" header + compact step
			content.WriteString(lipgloss.NewStyle().Foreground(purple).Bold(true).Render("KYREX (active)"))
			content.WriteString("\n")
			content.WriteString(lipgloss.NewStyle().Foreground(fg).Width(width).Render(currentStep))
			content.WriteString("\n\n")
		}
	}

	// 3. Tool telemetry (compact — with checkmarks)
	telemetry := m.RenderToolTelemetryCompact(width)
	if telemetry != "" {
		content.WriteString(telemetryStyle.Width(width).Render(telemetry) + "\n")
	}

	// 4. Mission summary (if available)
	if m.MissionSummary != "" {
		content.WriteString(missionSummaryStyle.Width(width).Render(m.MissionSummary))
		content.WriteString("\n")
	}

	return content.String()
}

// RenderToolTelemetryCompact renders tool calls with checkmarks for completed items.
// Used during active execution for a cleaner view.
func (m *Model) RenderToolTelemetryCompact(width int) string {
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

		// Add checkmark for completed tools
		checkmark := ""
		if e.State == ToolStateSuccess {
			checkmark = "✓ "
		} else if e.State == ToolStateFailed {
			checkmark = "✗ "
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
			Render(fmt.Sprintf("%s%s [%s] %s %s %s", checkmark, icon, durationStr, name, args, result))
		lines = append(lines, line)
	}

	return strings.Join(lines, "\n")
}
