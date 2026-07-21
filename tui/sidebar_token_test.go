package tui

import (
	"strings"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

// TestSidebarContextUsage verifies that the sidebar CONTEXT section renders the
// real context estimate, usage percent, and spent cost.
func TestSidebarContextUsage(t *testing.T) {
	m := NewModel(nil)
	m.Width = 120
	m.Height = 40
	m.ShowSidebar = true
	m.HasSentFirstMessage = true
	m.Sidebar.EngineStatus = "online"
	m._usageStats = map[string]interface{}{
		"current_context_est": 8000,
		"context_limit":       200000,
		"cost":                "≈$0.12",
	}
	m.applyLayout(m.recalculateLayout())

	out := m.View()

	for _, want := range []string{"CONTEXT", "8,000 tokens", "4% used", "$0.12 spent"} {
		if !strings.Contains(out, want) {
			t.Fatalf("sidebar output missing %q; rendered:\n%s", want, out)
		}
	}
}

// TestSidebarContextUsageEmpty verifies the fallback lines when no usage stats
// are available yet.
func TestSidebarContextUsageEmpty(t *testing.T) {
	m := NewModel(nil)
	m.Width = 120
	m.Height = 40
	m.ShowSidebar = true
	m.HasSentFirstMessage = true
	m.Sidebar.EngineStatus = "online"
	m.applyLayout(m.recalculateLayout())

	out := m.View()

	for _, want := range []string{"CONTEXT", "0 tokens", "0% used", "$0.00 spent"} {
		if !strings.Contains(out, want) {
			t.Fatalf("sidebar output missing %q; rendered:\n%s", want, out)
		}
	}
}

// TestSidebarContextUsagePersistsAfterClosingOverlay verifies that closing the
// /usage overlay with esc does not clear the stats — the sidebar keeps showing
// the last context and cost.
func TestSidebarContextUsagePersistsAfterClosingOverlay(t *testing.T) {
	m := NewModel(nil)
	m.Width = 120
	m.Height = 40
	m.ShowSidebar = true
	m.HasSentFirstMessage = true
	m.Sidebar.EngineStatus = "online"
	m._usageStats = map[string]interface{}{
		"current_context_est": 8000,
		"context_limit":       200000,
		"cost":                "≈$0.12",
	}
	m._usageOverlayActive = true
	m.applyLayout(m.recalculateLayout())

	m, _, _ = m.handleKeyMsg(tea.KeyMsg{Type: tea.KeyEsc}, time.Time{})

	if m._usageStats == nil {
		t.Fatal("closing usage overlay should not clear _usageStats")
	}

	out := m.View()
	if !strings.Contains(out, "8,000 tokens") || !strings.Contains(out, "$0.12 spent") {
		t.Fatalf("sidebar should still show last context/cost after closing overlay; rendered:\n%s", out)
	}
}
