package components

import (
	"fmt"
	"strings"

	"github.com/charmbracelet/lipgloss"
)

// ── Diff Renderer Styles ──
// Defined here to avoid circular imports with tui package.

var (
	// File header bar
	diffHeaderStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#7aa2f7")).
			Bold(true).
			Padding(0, 1)

	diffHeaderPath = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#bb9af7")).
			Bold(true)

	diffHeaderSummary = lipgloss.NewStyle().
				Foreground(lipgloss.Color("#9aa5ce"))

	// Gutter (line numbers)
	diffGutterStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#3b4261"))

	// Context lines
	diffContextStyle = lipgloss.NewStyle().
				Foreground(lipgloss.Color("#a9b1d6"))

	// Remove lines
	diffRemoveStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#f7768e")).
			Background(lipgloss.Color("#2d1520"))

	diffRemoveWordStyle = lipgloss.NewStyle().
				Foreground(lipgloss.Color("#ff3366")).
				Background(lipgloss.Color("#3d1a28")).
				Bold(true)

	// Add lines
	diffAddStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#9ece6a")).
			Background(lipgloss.Color("#152d1a"))

	diffAddWordStyle = lipgloss.NewStyle().
				Foreground(lipgloss.Color("#66ff99")).
				Background(lipgloss.Color("#1a3d20")).
				Bold(true)

	// Separator
	diffSeparator = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#3b4261"))

	// Status badges
	diffStatusPending = lipgloss.NewStyle().
				Foreground(lipgloss.Color("#e0af68")).
				Bold(true)

	diffStatusApproved = lipgloss.NewStyle().
				Foreground(lipgloss.Color("#9ece6a")).
				Bold(true)

	diffStatusRejected = lipgloss.NewStyle().
				Foreground(lipgloss.Color("#f7768e")).
				Bold(true)

	diffStatusApplied = lipgloss.NewStyle().
				Foreground(lipgloss.Color("#7aa2f7")).
				Bold(true)

	// Collapsed summary
	diffCollapsedStyle = lipgloss.NewStyle().
				Foreground(lipgloss.Color("#565f89")).
				Italic(true)

	// Border
	diffBorder = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#2f3346"))

	// Empty line placeholder
	diffEmptyStyle = lipgloss.NewStyle().
			Background(lipgloss.Color("#1a1b26"))
)

// renderRow represents one visual row in the side-by-side diff
type renderRow struct {
	leftNum      string // Old line number (formatted)
	leftContent  string // Left pane content (styled)
	rightNum     string // New line number (formatted)
	rightContent string // Right pane content (styled)
	isHeader     bool   // If true, render as full-width header
	headerText   string // Header text for full-width rows
}

// RenderDiffPane renders a complete side-by-side diff pane
func RenderDiffPane(block *DiffBlock, width int, _ int) string {
	if block == nil || width < 20 || len(block.Hunks) == 0 {
		return ""
	}

	var sb strings.Builder

	// ── Header Bar ──
	sb.WriteString(renderDiffHeader(block, width))
	sb.WriteString("\n")

	// ── Separator ──
	sb.WriteString(diffBorder.Render(strings.Repeat("─", width)))
	sb.WriteString("\n")

	// ── Calculate pane widths ──
	// Layout: [gutter][content] │ [gutter][content]
	// gutter = " 999 " = 5 chars (supports up to 9999 lines)
	// separator = 1 char
	gutterWidth := 5
	separatorWidth := 1
	contentWidth := (width - separatorWidth - (gutterWidth * 2)) / 2
	if contentWidth < 10 {
		contentWidth = 10
	}

	// ── Render Hunks ──
	for hunkIdx, hunk := range block.Hunks {
		// Hunk separator between hunks
		if hunkIdx > 0 {
			sb.WriteString(diffBorder.Render(strings.Repeat("·", width)))
			sb.WriteString("\n")
		}

		rows := buildRenderRows(&hunk, gutterWidth, contentWidth)
		for _, row := range rows {
			if row.isHeader {
				sb.WriteString(row.headerText)
				sb.WriteString("\n")
				continue
			}
			sb.WriteString(formatDiffRow(row, gutterWidth, contentWidth))
			sb.WriteString("\n")
		}
	}

	// ── Bottom border ──
	sb.WriteString(diffBorder.Render(strings.Repeat("─", width)))

	return sb.String()
}

// renderDiffHeader creates the file path header bar
func renderDiffHeader(block *DiffBlock, width int) string {
	icon := "📄"
	if block.Status == DiffStatusApproved {
		icon = "✓"
	} else if block.Status == DiffStatusRejected {
		icon = "✗"
	}

	path := block.FilePath
	if len(path) > width/2 {
		// Truncate long paths, keeping the filename
		parts := strings.Split(path, "/")
		if len(parts) > 2 {
			path = "…/" + strings.Join(parts[len(parts)-2:], "/")
		}
	}

	pathRendered := diffHeaderPath.Render(icon + " " + path)
	summary := renderStatusBadge(block)

	// Pad to fill width
	pathWidth := lipgloss.Width(pathRendered)
	summaryWidth := lipgloss.Width(summary)
	padding := width - pathWidth - summaryWidth - 2 // 2 for left/right padding
	if padding < 1 {
		padding = 1
	}

	return diffHeaderStyle.Width(width).Render(
		pathRendered + strings.Repeat(" ", padding) + summary,
	)
}

// renderStatusBadge returns a styled status indicator
func renderStatusBadge(block *DiffBlock) string {
	switch block.Status {
	case DiffStatusPending:
		return diffStatusPending.Render("● pending")
	case DiffStatusApproved:
		return diffStatusApproved.Render("✓ approved")
	case DiffStatusRejected:
		return diffStatusRejected.Render("✗ rejected")
	case DiffStatusApplied:
		return diffStatusApplied.Render("◆ applied")
	default:
		return diffHeaderSummary.Render("○")
	}
}

// buildRenderRows converts hunk lines into side-by-side render rows
func buildRenderRows(hunk *DiffHunk, gutterWidth, contentWidth int) []renderRow {
	var rows []renderRow
	lines := hunk.Lines
	i := 0

	for i < len(lines) {
		line := &lines[i]

		switch line.Type {
		case DiffLineRemove:
			// Check if next line is an add (paired change)
			if i+1 < len(lines) && lines[i+1].Type == DiffLineAdd {
				addLine := &lines[i+1]
				rows = append(rows, renderRow{
					leftNum:      formatGutter(line.OldLineNum, gutterWidth),
					leftContent:  renderLineWithWordChanges(line.Content, line.WordChanges, contentWidth, true),
					rightNum:     formatGutter(addLine.NewLineNum, gutterWidth),
					rightContent: renderLineWithWordChanges(addLine.Content, addLine.WordChanges, contentWidth, false),
				})
				i += 2
			} else {
				// Standalone remove
				rows = append(rows, renderRow{
					leftNum:      formatGutter(line.OldLineNum, gutterWidth),
					leftContent:  renderLineWithWordChanges(line.Content, line.WordChanges, contentWidth, true),
					rightNum:     formatGutter(0, gutterWidth),
					rightContent: renderEmptyLine(contentWidth),
				})
				i++
			}

		case DiffLineAdd:
			// Standalone add (not preceded by remove)
			rows = append(rows, renderRow{
				leftNum:      formatGutter(0, gutterWidth),
				leftContent:  renderEmptyLine(contentWidth),
				rightNum:     formatGutter(line.NewLineNum, gutterWidth),
				rightContent: renderLineWithWordChanges(line.Content, line.WordChanges, contentWidth, false),
			})
			i++

		case DiffLineContext:
			rows = append(rows, renderRow{
				leftNum:      formatGutter(line.OldLineNum, gutterWidth),
				leftContent:  renderContextLine(line.Content, contentWidth),
				rightNum:     formatGutter(line.NewLineNum, gutterWidth),
				rightContent: renderContextLine(line.Content, contentWidth),
			})
			i++

		default:
			i++
		}
	}

	return rows
}

// formatDiffRow renders a single side-by-side row
func formatDiffRow(row renderRow, gutterWidth, contentWidth int) string {
	sep := diffSeparator.Render("│")
	left := row.leftNum + " " + row.leftContent
	right := row.rightNum + " " + row.rightContent
	return left + " " + sep + " " + right
}

// formatGutter formats a line number for the gutter
func formatGutter(num int, width int) string {
	if num == 0 {
		return diffGutterStyle.Render(strings.Repeat(" ", width))
	}
	s := fmt.Sprintf("%d", num)
	if len(s) > width {
		s = s[len(s)-width:]
	}
	return diffGutterStyle.Render(fmt.Sprintf("%*s", width, s))
}

// renderContextLine renders an unchanged context line
func renderContextLine(content string, maxWidth int) string {
	truncated := truncateToWidth(content, maxWidth)
	return diffContextStyle.Render(truncated + padToWidth(truncated, maxWidth))
}

// renderEmptyLine renders a blank line (for remove/add alignment)
func renderEmptyLine(width int) string {
	return diffEmptyStyle.Render(strings.Repeat(" ", width))
}

// renderLineWithWordChanges renders a changed line with word-level highlighting
func renderLineWithWordChanges(content string, changes []WordChange, maxWidth int, isRemove bool) string {
	if len(changes) == 0 {
		// No word-level changes — render entire line with line style
		truncated := truncateToWidth(content, maxWidth)
		style := diffRemoveStyle
		if !isRemove {
			style = diffAddStyle
		}
		return style.Render(truncated + padToWidth(truncated, maxWidth))
	}

	// Build styled segments
	var sb strings.Builder
	baseStyle := diffRemoveStyle
	wordStyle := diffRemoveWordStyle
	if !isRemove {
		baseStyle = diffAddStyle
		wordStyle = diffAddWordStyle
	}

	runes := []rune(content)
	pos := 0

	for _, change := range changes {
		start := change.Start
		end := change.End

		// Clamp to rune length
		if start > len(runes) {
			start = len(runes)
		}
		if end > len(runes) {
			end = len(runes)
		}
		if start < pos {
			start = pos
		}
		if end < start {
			end = start
		}

		// Render unchanged segment before this change
		if start > pos {
			segment := string(runes[pos:start])
			sb.WriteString(baseStyle.Render(segment))
		}

		// Render changed word
		if end > start {
			segment := string(runes[start:end])
			sb.WriteString(wordStyle.Render(segment))
		}

		pos = end
	}

	// Render remaining unchanged segment
	if pos < len(runes) {
		segment := string(runes[pos:])
		sb.WriteString(baseStyle.Render(segment))
	}

	result := sb.String()
	resultWidth := lipgloss.Width(result)

	// Pad to fill content width
	if resultWidth < maxWidth {
		result += baseStyle.Render(strings.Repeat(" ", maxWidth-resultWidth))
	}

	return result
}

// RenderDiffSummary renders a collapsed one-line summary of a diff block
func RenderDiffSummary(block *DiffBlock) string {
	if block == nil {
		return ""
	}

	icon := "📄"
	switch block.Status {
	case DiffStatusApproved:
		icon = "✓"
	case DiffStatusRejected:
		icon = "✗"
	case DiffStatusApplied:
		icon = "◆"
	}

	summary := block.Summary()
	status := statusString(block.Status)

	return diffCollapsedStyle.Render(
		fmt.Sprintf("%s %s  %s  %s", icon, block.FilePath, summary, status),
	)
}

// statusString returns a human-readable status label
func statusString(status DiffStatus) string {
	switch status {
	case DiffStatusPending:
		return "● pending"
	case DiffStatusApproved:
		return "✓ approved"
	case DiffStatusRejected:
		return "✗ rejected"
	case DiffStatusApplied:
		return "◆ applied"
	default:
		return "○"
	}
}

// RenderDiffBlocks renders all diff blocks for inline viewport display
func RenderDiffBlocks(blocks []DiffBlock, width int) string {
	if len(blocks) == 0 || width < 20 {
		return ""
	}

	var sb strings.Builder
	for i := range blocks {
		block := &blocks[i]
		if block.Collapsed {
			sb.WriteString(RenderDiffSummary(block))
		} else {
			sb.WriteString(RenderDiffPane(block, width, 0))
		}
		sb.WriteString("\n\n")
	}
	return sb.String()
}

// ── Utility Functions ──

// truncateToWidth truncates a string to fit within maxWidth display columns
func truncateToWidth(s string, maxWidth int) string {
	if maxWidth <= 0 {
		return ""
	}
	runes := []rune(s)
	if len(runes) <= maxWidth {
		return s
	}
	if maxWidth <= 1 {
		return "…"
	}
	return string(runes[:maxWidth-1]) + "…"
}

// padToWidth returns spaces needed to pad a string to maxWidth
func padToWidth(s string, maxWidth int) string {
	w := lipgloss.Width(s)
	if w >= maxWidth {
		return ""
	}
	return strings.Repeat(" ", maxWidth-w)
}

// ── Phase 2: Line-Matching Algorithm ──

type MatchedRow struct {
	Left     string
	Right    string
	LeftNum  int
	RightNum int
	IsChange bool
}

func MatchLines(oldLines, newLines []string) []MatchedRow {
	if len(oldLines) == 0 && len(newLines) == 0 {
		return nil
	}
	if len(oldLines) == 0 {
		rows := make([]MatchedRow, len(newLines))
		for i, s := range newLines {
			rows[i] = MatchedRow{Right: s, RightNum: i + 1, IsChange: true}
		}
		return rows
	}
	if len(newLines) == 0 {
		rows := make([]MatchedRow, len(oldLines))
		for i, s := range oldLines {
			rows[i] = MatchedRow{Left: s, LeftNum: i + 1, IsChange: true}
		}
		return rows
	}

	ops := computeEditOps(oldLines, newLines)
	var rows []MatchedRow
	oldIdx, newIdx := 0, 0

	for _, op := range ops {
		switch op.tag {
		case "equal":
			count := op.iEnd - op.iStart
			for k := 0; k < count; k++ {
				rows = append(rows, MatchedRow{
					Left:     oldLines[oldIdx],
					Right:    newLines[newIdx],
					LeftNum:  oldIdx + 1,
					RightNum: newIdx + 1,
				})
				oldIdx++
				newIdx++
			}
		case "replace":
			oldCount := op.iEnd - op.iStart
			newCount := op.jEnd - op.jStart
			maxLen := oldCount
			if newCount > maxLen {
				maxLen = newCount
			}
			for k := 0; k < maxLen; k++ {
				row := MatchedRow{IsChange: true}
				if k < oldCount {
					row.Left = oldLines[oldIdx]
					row.LeftNum = oldIdx + 1
					oldIdx++
				}
				if k < newCount {
					row.Right = newLines[newIdx]
					row.RightNum = newIdx + 1
					newIdx++
				}
				rows = append(rows, row)
			}
		case "delete":
			count := op.iEnd - op.iStart
			for k := 0; k < count; k++ {
				rows = append(rows, MatchedRow{
					Left:     oldLines[oldIdx],
					Right:    "",
					LeftNum:  oldIdx + 1,
					RightNum: 0,
					IsChange: true,
				})
				oldIdx++
			}
		case "insert":
			count := op.jEnd - op.jStart
			for k := 0; k < count; k++ {
				rows = append(rows, MatchedRow{
					Left:     "",
					Right:    newLines[newIdx],
					LeftNum:  0,
					RightNum: newIdx + 1,
					IsChange: true,
				})
				newIdx++
			}
		}
	}
	return rows
}

type editOp struct {
	tag          string
	iStart, iEnd int
	jStart, jEnd int
}

func computeEditOps(oldLines, newLines []string) []editOp {
	m, n := len(oldLines), len(newLines)

	dp := make([][]int, m+1)
	for i := range dp {
		dp[i] = make([]int, n+1)
	}
	for i := 1; i <= m; i++ {
		for j := 1; j <= n; j++ {
			if oldLines[i-1] == newLines[j-1] {
				dp[i][j] = dp[i-1][j-1] + 1
			} else if dp[i-1][j] >= dp[i][j-1] {
				dp[i][j] = dp[i-1][j]
			} else {
				dp[i][j] = dp[i][j-1]
			}
		}
	}

	type rawStep struct {
		tag string
		old int
		new int
	}
	var stack []rawStep
	i, j := m, n
	for i > 0 || j > 0 {
		if i > 0 && j > 0 && oldLines[i-1] == newLines[j-1] {
			stack = append(stack, rawStep{"equal", i - 1, j - 1})
			i--
			j--
		} else if i > 0 && (j == 0 || dp[i-1][j] >= dp[i][j-1]) {
			stack = append(stack, rawStep{"delete", i - 1, -1})
			i--
		} else {
			stack = append(stack, rawStep{"insert", -1, j - 1})
			j--
		}
	}

	steps := make([]rawStep, len(stack))
	for idx, s := range stack {
		steps[len(stack)-1-idx] = s
	}

	var ops []editOp
	for _, s := range steps {
		if len(ops) > 0 {
			last := &ops[len(ops)-1]
			switch {
			case last.tag == s.tag && s.tag == "equal":
				last.iEnd++
				last.jEnd++
				continue
			case last.tag == "delete" && s.tag == "insert":
				last.tag = "replace"
				last.jStart = s.new
				last.jEnd = s.new + 1
				continue
			case last.tag == "insert" && s.tag == "delete":
				last.tag = "replace"
				last.iStart = s.old
				last.iEnd = s.old + 1
				continue
			case last.tag == "replace" && s.tag == "delete":
				last.iEnd++
				continue
			case last.tag == "replace" && s.tag == "insert":
				last.jEnd++
				continue
			}
		}
		op := editOp{tag: s.tag}
		switch s.tag {
		case "equal":
			op.iStart, op.iEnd = s.old, s.old+1
			op.jStart, op.jEnd = s.new, s.new+1
		case "delete":
			op.iStart, op.iEnd = s.old, s.old+1
			op.jStart, op.jEnd = 0, 0
		case "insert":
			op.iStart, op.iEnd = 0, 0
			op.jStart, op.jEnd = s.new, s.new+1
		}
		ops = append(ops, op)
	}

	return ops
}

// ── Phase 3: Streaming Side-by-Side Column Rendering ──

func RenderSideBySideStream(blocks []DiffBlock, width int) string {
	if len(blocks) == 0 || width < 20 {
		return ""
	}

	separatorWidth := 3
	leftWidth := (width - separatorWidth) / 2
	rightWidth := width - leftWidth - separatorWidth
	if leftWidth < 5 {
		leftWidth = 5
	}
	if rightWidth < 5 {
		rightWidth = 5
	}

	redStyle := lipgloss.NewStyle().Foreground(lipgloss.Color("#f7768e")).Width(leftWidth)
	greenStyle := lipgloss.NewStyle().Foreground(lipgloss.Color("#9ece6a")).Width(rightWidth)
	dimStyle := lipgloss.NewStyle().Foreground(lipgloss.Color("#565f89"))
	sepStyle := lipgloss.NewStyle().Foreground(lipgloss.Color("#3b4261"))

	leftCellStyle := lipgloss.NewStyle().Width(leftWidth)
	rightCellStyle := lipgloss.NewStyle().Width(rightWidth)

	var sb strings.Builder

	for blockIdx, block := range blocks {
		if blockIdx > 0 {
			sb.WriteString(dimStyle.Render(strings.Repeat("─", width)))
			sb.WriteString("\n")
		}

		header := fmt.Sprintf(" %s ", block.FilePath)
		summary := block.Summary()
		headerLine := diffHeaderPath.Render(header) + " " + diffHeaderSummary.Render(summary)
		sb.WriteString(headerLine)
		sb.WriteString("\n")

		for _, hunk := range block.Hunks {
			var oldLines, newLines []string
			for _, line := range hunk.Lines {
				switch line.Type {
				case DiffLineContext:
					oldLines = append(oldLines, "  "+line.Content)
					newLines = append(newLines, "  "+line.Content)
				case DiffLineRemove:
					oldLines = append(oldLines, "- "+line.Content)
				case DiffLineAdd:
					newLines = append(newLines, "+ "+line.Content)
				}
			}

			matched := MatchLines(oldLines, newLines)
			for _, row := range matched {
				leftStr := row.Left
				rightStr := row.Right

				leftTrunc := truncateToDisplayWidth(leftStr, leftWidth)
				rightTrunc := truncateToDisplayWidth(rightStr, rightWidth)

				var leftRendered, rightRendered string
				if row.IsChange {
					if row.Left != "" {
						leftRendered = redStyle.Render(leftTrunc)
					} else {
						leftRendered = leftCellStyle.Render("")
					}
					if row.Right != "" {
						rightRendered = greenStyle.Render(rightTrunc)
					} else {
						rightRendered = rightCellStyle.Render("")
					}
				} else {
					leftRendered = dimStyle.Width(leftWidth).Render(leftTrunc)
					rightRendered = dimStyle.Width(rightWidth).Render(rightTrunc)
				}

				sb.WriteString(leftRendered)
				sb.WriteString(sepStyle.Render(" │ "))
				sb.WriteString(rightRendered)
				sb.WriteString("\n")
			}
		}
	}

	return sb.String()
}

func truncateToDisplayWidth(s string, maxWidth int) string {
	if maxWidth <= 0 {
		return ""
	}
	w := lipgloss.Width(s)
	if w <= maxWidth {
		return s
	}
	runes := []rune(s)
	currentWidth := 0
	for i, r := range runes {
		rw := lipgloss.Width(string(r))
		if currentWidth+rw > maxWidth-1 {
			return string(runes[:i]) + "…"
		}
		currentWidth += rw
	}
	return s
}
