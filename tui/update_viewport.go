package tui

import (
	"fmt"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
	"github.com/kp84-hub/kx/tui/components"
)

// GetSelectedText extracts clean text from the visible viewport selection.
// Works on visible lines (screen coordinates) and strips ANSI codes.
func (m Model) GetSelectedText() string {
	if m.SelectStart == m.SelectEnd {
		return ""
	}

	// Get the viewport's current rendered view (what's actually visible on screen)
	view := m.Viewport.View()
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
		runes := []rune(cleanLine)

		colStart := 0
		if lineIdx == start.Line {
			colStart = start.Col
		}
		colEnd := len(runes)
		if lineIdx == end.Line {
			colEnd = end.Col
		}

		// Clamp to valid range
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
			emit(lipgloss.NewStyle().Foreground(accent).Bold(true).Render("> You"))
			emitBlock(strings.Split(style.Render(turn.userMsg), "\n"))
			content.WriteString("\n")
			absLine++
		}

		if turn.thinking != "" {
			emit(lipgloss.NewStyle().Foreground(thinkingC).Italic(true).Render("\U000f024b  Thought"))
			emitBlock(strings.Split(thinkingStyle.Width(width-2).Render(turn.thinking), "\n"))
			emit(separatorStyle.Width(width).Render("────────────────────────────────────────────────"))
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
			emit(lipgloss.NewStyle().Foreground(purple).Bold(true).Render("KYREX"))
			emitBlock(strings.Split(lipgloss.NewStyle().Foreground(fg).Width(width).Render(other), "\n"))
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

	emit(lipgloss.NewStyle().Foreground(thinkingC).Italic(true).Render("\U000f024b  Thought"))
	for _, tl := range strings.Split(thinkingStyle.Width(width-2).Render(m.Reasoning), "\n") {
		emit(tl)
	}
	emit(separatorStyle.Width(width).Render("────────────────────────────────────────────────"))

	return content.String()
}

// FullViewportContent builds the complete viewport buffer with selection highlights applied.
// Incremental rendering: stable history is cached and only rebuilt when it changes.
// During streaming, only the dynamic tail (reasoning/tokens/telemetry) is re-rendered.
func (m *Model) FullViewportContent(width int) string {
	fvcStart := time.Now()
	
	// Check if stable history cache is still valid
	historyLen := len(m.History)
	historyContent := ""
	historyLines := 0
	cacheHit := false

	if m._stableHistoryLen == historyLen &&
		m._stableHistoryWidth == width &&
		m._stableHistoryContent != "" {
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
		emit(lipgloss.NewStyle().Foreground(thinkingC).Italic(true).Render("\U000f024b  Thought"))
		for _, tl := range strings.Split(thinkingStyle.Width(width-2).Render(m.Reasoning), "\n") {
			emit(tl)
		}
		emit(separatorStyle.Width(width).Render("────────────────────────────────────────────────"))
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
		emit(lipgloss.NewStyle().Foreground(purple).Bold(true).Render("KYREX"))
		emitBlock(strings.Split(lipgloss.NewStyle().Foreground(fg).Width(width).Render(m.CurrToken), "\n"))
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
