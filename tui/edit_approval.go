package tui

import (
	"fmt"
	"strings"

	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/x/ansi"
)

func (m Model) isEditApproval() bool {
	if m.ConfirmID == "" {
		return false
	}
	switch m.ConfirmType {
	case "", "edit", "diff":
		return strings.HasPrefix(m.ConfirmDiff, "--- ") ||
			strings.HasPrefix(m.ConfirmDiff, "diff --git ") ||
			strings.HasPrefix(m.ConfirmDiff, "@@ ")
	default:
		return false
	}
}

func (m Model) editApprovalChrome(expanded bool) (string, string) {
	title := lipgloss.NewStyle().Foreground(purple).Bold(true).Render(ansi.Hardwrap("Review file change", m.Width, true))
	path := ansi.Strip(displayPath(m.ConfirmPath))
	details := "d Details"
	if expanded {
		details = "d Overview"
	}
	if pathWidth := ansi.StringWidth(path); pathWidth > m.Width {
		path = ansi.TruncateLeft(path, pathWidth-m.Width+1, "…")
	}
	controls := ansi.Hardwrap("y Apply  n Reject  "+details, m.Width, true)
	return title + "\n" + ansi.Hardwrap(path, m.Width, true), controls
}

func (m Model) renderEditApproval() string {
	header, controls := m.editApprovalChrome(m.confirmDetails != nil)
	if m.confirmDetails != nil {
		return lipgloss.JoinVertical(lipgloss.Left, header, m.confirmDetails.View(), controls)
	}

	added, removed := 0, 0
	inHunk := false
	for _, line := range strings.Split(m.ConfirmDiff, "\n") {
		switch {
		case strings.HasPrefix(line, "@@ "):
			inHunk = true
		case inHunk && strings.HasPrefix(line, "+"):
			added++
		case inHunk && strings.HasPrefix(line, "-"):
			removed++
		}
	}
	summary := ansi.Hardwrap(fmt.Sprintf("%d lines added, %d removed", added, removed), m.Width, true)
	return lipgloss.JoinVertical(lipgloss.Left, header, summary, controls)
}

func (m Model) layoutEditApprovalDetails(offset int) *viewport.Model {
	header, controls := m.editApprovalChrome(true)
	height := max(1, m.Height-lipgloss.Height(header)-lipgloss.Height(controls))
	details := viewport.New(m.Width, height)
	diff := strings.ReplaceAll(ansi.Strip(m.ConfirmDiff), "\t", "    ")
	if path := ansi.Strip(m.ConfirmPath); ansi.StringWidth(displayPath(path)) > m.Width {
		diff = path + "\n" + diff
	}
	details.SetContent(ansi.Hardwrap(diff, m.Width, true))
	details.SetYOffset(offset)
	return &details
}

func (m Model) updateEditApprovalDetails(msg tea.Msg) (Model, tea.Cmd) {
	details, cmd := m.confirmDetails.Update(msg)
	m.confirmDetails = &details
	return m, cmd
}
