package tui

import (
	"fmt"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

func TestEditApprovalOverviewOpensScrollableDetails(t *testing.T) {
	var decisions []interface{}
	m := NewModel(func(payload interface{}) error {
		decisions = append(decisions, payload)
		return nil
	})
	const width, height = 32, 11
	m.Width, m.Height = width, height
	var patch strings.Builder
	patch.WriteString("--- a/index.html\n+++ b/index.html\n@@ -1 +1,24 @@\n-old line\n")
	for i := 1; i <= 24; i++ {
		fmt.Fprintf(&patch, "+patch line %02d with details to wrap\n", i)
	}
	m, _, _ = m.handleConfirmRequest(MsgFromEngine{
		ID: "edit-1", Value: "edit", Path: "/long/path/to/project/index.html", Diff: patch.String(),
	})
	overview := m.View()
	if !strings.Contains(overview, "index.html") ||
		!strings.Contains(overview, "24 lines added, 1 removed") ||
		!strings.Contains(overview, "d Details") || strings.Contains(overview, "patch line") ||
		lipgloss.Height(overview) > 5 || lipgloss.Width(overview) > width {
		t.Fatalf("expected a compact summary, got:\n%s", overview)
	}

	key := func(r rune) tea.KeyMsg { return tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{r}} }
	next, _ := m.Update(key('d'))
	m = next.(Model)
	details := m.View()
	if !strings.Contains(details, "patch line 01") ||
		!strings.Contains(details, "d Overview") || lipgloss.Height(details) > height ||
		lipgloss.Width(details) > width {
		t.Fatalf("expected the start of a fitting diff, got:\n%s", details)
	}
	next, _ = m.Update(tea.KeyMsg{Type: tea.KeyPgDown})
	m = next.(Model)
	if m.confirmDetails.YOffset == 0 {
		t.Fatal("Page Down should scroll the approval diff")
	}
	next, _ = m.Update(tea.WindowSizeMsg{Width: 39, Height: 13})
	m = next.(Model)
	if m.confirmDetails.Width != 39 || lipgloss.Height(m.View()) > 13 {
		t.Fatalf("details did not follow the new terminal dimensions:\n%s", m.View())
	}
	next, _ = m.Update(key('d'))
	m = next.(Model)
	if m.confirmDetails != nil || len(decisions) != 0 {
		t.Fatalf("returning to overview changed the approval: %#v", decisions)
	}
	next, _ = m.Update(key('d'))
	m = next.(Model)
	m, _, _ = m.handleConfirmRequest(MsgFromEngine{
		ID: "edit-2", Value: "edit", Path: "/project/another.html", Diff: patch.String(),
	})
	if m.confirmDetails != nil {
		t.Fatal("a replacement approval must start in the overview")
	}
	next, _ = m.Update(key('n'))
	m = next.(Model)
	last := decisions[len(decisions)-1].(map[string]interface{})
	if m.ConfirmID != "" || last["id"] != "edit-2" || last["approved"] != false {
		t.Fatalf("reject should settle the current approval: %#v", decisions)
	}
}
