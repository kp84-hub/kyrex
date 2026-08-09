package tui

import (
	"reflect"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

func testMCPConnectors() []MCPConnector {
	return []MCPConnector{
		{
			ID: "filesystem", Name: "Filesystem", Description: "Local files",
			Command: "npx", Args: []string{"-y", "filesystem"},
			Auth: MCPConnectorAuth{Mode: "none"},
			Verification: MCPConnectorVerification{Status: "verified"},
		},
		{
			ID: "github", Name: "GitHub", Description: "Repositories",
			Command: "uvx", Auth: MCPConnectorAuth{Mode: "environment variable"},
			Verification: MCPConnectorVerification{Status: "needs_verification"},
		},
	}
}

func TestMCPPickerPauseDecodesConnectorRecords(t *testing.T) {
	connectors := testMCPConnectors()
	files := []interface{}{
		map[string]interface{}{
			"id": "filesystem", "name": "Filesystem", "description": "Local files",
			"command": "npx", "args": []interface{}{"-y", "filesystem"},
			"auth": map[string]interface{}{"mode": "none"},
			"verification": map[string]interface{}{"status": "verified"},
		},
	}

	m := NewModel(nil)
	m, _, handled := m.handlePause(MsgFromEngine{Value: "mcp_connector_picker", Files: files})
	if !handled || !m._mcpPickerActive {
		t.Fatal("expected MCP picker to activate")
	}
	if len(m._mcpPickerAllItems) != 1 || m._mcpPickerAllItems[0].Name != connectors[0].Name {
		t.Fatalf("unexpected decoded connectors: %+v", m._mcpPickerAllItems)
	}
	if !reflect.DeepEqual(m._mcpPickerItems, m._mcpPickerAllItems) {
		t.Fatalf("items should initially equal all items: %+v", m._mcpPickerItems)
	}
}

func TestMCPPickerNavigationFilteringAndCancellation(t *testing.T) {
	m := NewModel(nil)
	m._mcpPickerActive = true
	m._mcpPickerAllItems = testMCPConnectors()
	m._mcpPickerItems = append([]MCPConnector(nil), m._mcpPickerAllItems...)

	m, _, handled := m.handleMCPPickerKey(tea.KeyMsg{Type: tea.KeyDown})
	if !handled || m._mcpPickerIndex != 1 {
		t.Fatalf("down should select second connector, handled=%v index=%d", handled, m._mcpPickerIndex)
	}
	m, _, handled = m.handleMCPPickerKey(tea.KeyMsg{Type: tea.KeyDown})
	if !handled || m._mcpPickerIndex != 0 {
		t.Fatalf("down should wrap to first connector, handled=%v index=%d", handled, m._mcpPickerIndex)
	}
	m, _, handled = m.handleMCPPickerKey(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'g'}})
	if !handled || len(m._mcpPickerItems) != 1 || m._mcpPickerItems[0].ID != "github" {
		t.Fatalf("filter should retain GitHub: handled=%v items=%+v", handled, m._mcpPickerItems)
	}
	m, _, handled = m.handleMCPPickerKey(tea.KeyMsg{Type: tea.KeyBackspace})
	if !handled || len(m._mcpPickerItems) != 2 || m._mcpPickerFilter != "" {
		t.Fatalf("backspace should clear filter: handled=%v filter=%q items=%+v", handled, m._mcpPickerFilter, m._mcpPickerItems)
	}
	m, _, handled = m.handleMCPPickerKey(tea.KeyMsg{Type: tea.KeyEnter})
	if !handled || !m._mcpPickerActive {
		t.Fatal("Enter should be handled without closing the picker")
	}
	m, _, handled = m.handleMCPPickerKey(tea.KeyMsg{Type: tea.KeyEsc})
	if !handled || m._mcpPickerActive || m._mcpPickerIndex != 0 || m._mcpPickerFilter != "" {
		t.Fatalf("Escape should cancel and reset picker: handled=%v active=%v index=%d filter=%q", handled, m._mcpPickerActive, m._mcpPickerIndex, m._mcpPickerFilter)
	}
}

func TestRenderMCPPickerShowsConnectorMetadata(t *testing.T) {
	m := NewModel(nil)
	m._mcpPickerActive = true
	m._mcpPickerAllItems = testMCPConnectors()
	m._mcpPickerItems = m._mcpPickerAllItems

	view := m.RenderMCPPicker()
	for _, want := range []string{"Filesystem", "Local files", "npx", "none", "verified", "GitHub", "environment variable", "needs_verification"} {
		if !contains(view, want) {
			t.Errorf("MCP picker view missing %q: %q", want, view)
		}
	}
}

func contains(s, want string) bool {
	return strings.Contains(s, want)
}
