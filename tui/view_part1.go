package tui

import (
	"fmt"
	"strings"

	"github.com/charmbracelet/lipgloss"
)

// ═══════════════════════════════════════════
// Sidebar Rendering — Complete Rebuild 0.1.4
// ═══════════════════════════════════════════

// RenderStatusBar — top bar with model, mode, token count, engine status dot
func (m Model) RenderStatusBar(width int) string {
	usableWidth := width - 2
	if usableWidth < 10 {
		usableWidth = 10
	}

	// Engine status dot
	dotColor := statusOnline
	dotLabel := "Online"
	if m.Sidebar.EngineStatus == "offline" {
		dotColor = statusOffline
		dotLabel = "Offline"
	} else if m.Sidebar.EngineStatus == "busy" || m.IsThinking || m.Sidebar.IsGenerating {
		dotColor = statusBusy
		dotLabel = "Busy"
	}
	dot := statusDotStyle.Foreground(dotColor).Render("●")
	statusText := lipgloss.NewStyle().Foreground(fgDim).Render(dotLabel)

	// Model
	model := m.Sidebar.CurrentModel
	if model == "unknown" && m.LLMInfo != "" {
		// Parse from LLMInfo
		parts := strings.SplitN(m.LLMInfo, " (", 2)
		if len(parts) > 0 {
			model = parts[0]
		}
	}
	modelLabel := sidebarLabel.Render("Model:")
	modelVal := sidebarValue.Render(truncate(model, 18))

	// Mode
	modeLabel := sidebarLabel.Render("Mode:")
	modeVal := sidebarValue.Render(m.Mode)
	if modeVal == "" {
		modeVal = sidebarValue.Render(string(m.Phase))
	}

	// Token count
	tokenLabel := sidebarLabel.Render("Tokens:")
	tokenVal := sidebarValue.Render(fmt.Sprintf("%d", m.Sidebar.TokenCount))

	// Build rows
	row1 := lipgloss.JoinHorizontal(lipgloss.Left,
		dot, " ", statusText,
		strings.Repeat(" ", usableWidth-18),
		modelLabel, " ", modelVal,
	)

	row2 := lipgloss.JoinHorizontal(lipgloss.Left,
		modeLabel, " ", modeVal,
		strings.Repeat(" ", usableWidth-22),
		tokenLabel, " ", tokenVal,
	)

	return statusBarStyle.Width(usableWidth).Render(
		lipgloss.JoinVertical(lipgloss.Left, row1, row2),
	)
}
