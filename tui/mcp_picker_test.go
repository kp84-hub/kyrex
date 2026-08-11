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
			ID: "playwright", Name: "Playwright MCP", Description: "Browser automation", Category: "Web",
			Command: "npx", Args: []string{"-y", "@playwright/mcp@latest"},
			Prerequisites: []string{"Node.js/npm"}, InstallationNotes: "Run with npx.",
			Auth:         MCPConnectorAuth{Mode: "none", Warning: "No credentials."},
			Verification: MCPConnectorVerification{Status: "verified"},
		},
		{
			ID: "github", Name: "GitHub MCP Server", Description: "Repositories", Category: "Development",
			Auth:         MCPConnectorAuth{Mode: "environment variable", Warning: "Authentication required.", RequiredEnvironment: []string{"GITHUB_PERSONAL_ACCESS_TOKEN"}},
			Verification: MCPConnectorVerification{Status: "requires_additional_configuration"},
		},
	}
}

func TestMCPPickerPauseDecodesConnectorRecords(t *testing.T) {
	connectors := testMCPConnectors()
	files := []interface{}{
		map[string]interface{}{
			"id": "playwright", "name": "Playwright MCP", "description": "Browser automation", "category": "Web",
			"command": "npx", "args": []interface{}{"-y", "@playwright/mcp@latest"}, "prerequisites": []interface{}{"Node.js/npm"},
			"installation_notes": "Run with npx.",
			"auth":               map[string]interface{}{"mode": "none", "warning": "No credentials.", "required_environment": []interface{}{}},
			"source_url":         "https://example.test/playwright", "verification": map[string]interface{}{"status": "verified", "checked_at": "authoritative-task-configuration"},
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
	for _, r := range []rune("hub") {
		m, _, handled = m.handleMCPPickerKey(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{r}})
		if !handled {
			t.Fatalf("rune %q should be handled while filtering", r)
		}
	}
	if len(m._mcpPickerItems) != 1 || m._mcpPickerItems[0].ID != "github" {
		t.Fatalf("filter \"hub\" should retain only GitHub: items=%+v", m._mcpPickerItems)
	}
	for i := 0; i < 3; i++ {
		m, _, handled = m.handleMCPPickerKey(tea.KeyMsg{Type: tea.KeyBackspace})
		if !handled {
			t.Fatalf("backspace %d should be handled while filtering", i+1)
		}
	}
	if len(m._mcpPickerItems) != 2 || m._mcpPickerFilter != "" {
		t.Fatalf("three backspaces should clear filter: filter=%q items=%+v", m._mcpPickerFilter, m._mcpPickerItems)
	}
	m, _, handled = m.handleMCPPickerKey(tea.KeyMsg{Type: tea.KeyEnter})
	if !handled || !m._mcpPickerActive || !m._mcpPickerDetail {
		t.Fatal("Enter should open the connector detail view")
	}
	m, _, handled = m.handleMCPPickerKey(tea.KeyMsg{Type: tea.KeyEsc})
	if !handled || !m._mcpPickerActive || m._mcpPickerDetail {
		t.Fatal("Escape from detail should return to the catalog")
	}
	m, _, handled = m.handleMCPPickerKey(tea.KeyMsg{Type: tea.KeyEsc})
	if !handled || m._mcpPickerActive || m._mcpPickerIndex != 0 || m._mcpPickerFilter != "" {
		t.Fatalf("Escape should cancel and reset picker: handled=%v active=%v index=%d filter=%q", handled, m._mcpPickerActive, m._mcpPickerIndex, m._mcpPickerFilter)
	}
}

func TestMCPPickerFilteringMatchesCategoryAndDescription(t *testing.T) {
	m := NewModel(nil)
	m._mcpPickerAllItems = testMCPConnectors()

	for _, tc := range []struct {
		filter string
		wantID string
	}{
		{"web", "playwright"},
		{"browser automation", "playwright"},
		{"repositories", "github"},
	} {
		got := filterMCPConnectors(m._mcpPickerAllItems, tc.filter)
		if len(got) != 1 || got[0].ID != tc.wantID {
			t.Fatalf("filter %q = %+v; want %s", tc.filter, got, tc.wantID)
		}
	}
}

func TestMCPPickerInstalledStateDecodesFromEnginePayload(t *testing.T) {
	m := NewModel(nil)
	m, _, handled := m.handlePause(MsgFromEngine{
		Value: "mcp_connector_picker",
		Files: []interface{}{map[string]interface{}{
			"id": "playwright", "name": "Playwright MCP", "description": "Browser automation", "category": "Web",
			"command": "npx", "args": []interface{}{"-y", "@playwright/mcp@latest"}, "prerequisites": []interface{}{"Node.js/npm"},
			"installation_notes": "Run with npx.", "auth": map[string]interface{}{"mode": "none", "warning": "No credentials.", "required_environment": []interface{}{}},
			"source_url": "https://example.test/playwright", "verification": map[string]interface{}{"status": "verified", "checked_at": "now"},
			"installed": true,
		}},
	})
	if !handled || len(m._mcpPickerAllItems) != 1 || !m._mcpPickerAllItems[0].Installed {
		t.Fatalf("expected installed state from engine payload: %+v", m._mcpPickerAllItems)
	}
}

func TestMCPPickerInstallAndRemoveCommands(t *testing.T) {
	var sent []map[string]string
	m := NewModel(func(message interface{}) error {
		if command, ok := message.(map[string]string); ok {
			sent = append(sent, command)
		}
		return nil
	})
	m._mcpPickerActive = true
	m._mcpPickerDetail = true
	m._mcpPickerItems = []MCPConnector{testMCPConnectors()[0]}
	m._mcpPickerIndex = 0

	m, _, handled := m.handleMCPPickerKey(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'i'}})
	if !handled || len(sent) != 1 || sent[0]["content"] != "/mcp install playwright" {
		t.Fatalf("install command = handled=%v sent=%v", handled, sent)
	}

	m._mcpPickerItems[0].Installed = true
	m, _, handled = m.handleMCPPickerKey(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'r'}})
	if !handled || len(sent) != 2 || sent[1]["content"] != "/mcp remove playwright" {
		t.Fatalf("remove command = handled=%v sent=%v", handled, sent)
	}
}

func TestMCPPickerTestConnectionSendsCommand(t *testing.T) {
	var sent []map[string]string
	m := NewModel(func(message interface{}) error {
		if command, ok := message.(map[string]string); ok {
			sent = append(sent, command)
		}
		return nil
	})
	m._mcpPickerActive = true
	m._mcpPickerDetail = true
	installed := testMCPConnectors()[0]
	installed.Installed = true
	m._mcpPickerItems = []MCPConnector{installed}

	m, _, handled := m.handleMCPPickerKey(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'t'}})
	if !handled || len(sent) != 1 || sent[0]["content"] != "/mcp test playwright" {
		t.Fatalf("test connection command = handled=%v sent=%v", handled, sent)
	}
	if !strings.Contains(m.Toast, "Testing Playwright MCP") {
		t.Fatalf("test connection toast = %q", m.Toast)
	}
}

func TestMCPPickerRendersConnectionResult(t *testing.T) {
	m := NewModel(nil)
	m._mcpPickerActive = true
	m._mcpPickerDetail = true
	installed := testMCPConnectors()[0]
	installed.Installed = true
	m._mcpPickerItems = []MCPConnector{installed}

	m, _, handled := m.handlePause(MsgFromEngine{
		Type: "tui_pause", Value: "mcp_connection_result",
		Files: map[string]interface{}{"success": true, "server": "playwright", "tool_count": 3},
	})
	if !handled || m._mcpTestResult == nil || m._mcpTestResult.ToolCount != 3 {
		t.Fatalf("connection result = handled=%v result=%+v", handled, m._mcpTestResult)
	}
	if !strings.Contains(m.RenderMCPPicker(), "3 tool(s) discovered") {
		t.Fatalf("detail view did not render tool count: %q", m.RenderMCPPicker())
	}
}

func TestRenderMCPPickerShowsConnectorMetadata(t *testing.T) {
	m := NewModel(nil)
	m._mcpPickerActive = true
	m._mcpPickerAllItems = testMCPConnectors()
	m._mcpPickerItems = m._mcpPickerAllItems

	view := m.RenderMCPPicker()
	for _, want := range []string{"Playwright MCP", "Browser automation", "AVAILABLE", "Web", "GitHub MCP Server", "Repositories", "Development"} {
		if !contains(view, want) {
			t.Errorf("MCP picker main view missing compact entry %q: %q", want, view)
		}
	}
	for _, notWant := range []string{"npx", "none", "verified", "environment variable", "requires_additional_configuration"} {
		if contains(view, notWant) {
			t.Errorf("MCP picker main view unexpectedly contains detail metadata %q: %q", notWant, view)
		}
	}

	m._mcpPickerDetail = true
	detail := m.RenderMCPPicker()
	for _, want := range []string{"npx", "none", "Playwright MCP"} {
		if !contains(detail, want) {
			t.Errorf("MCP picker Playwright detail view missing connector metadata %q: %q", want, detail)
		}
	}

	m._mcpPickerIndex = 1
	detail = m.RenderMCPPicker()
	for _, want := range []string{"GitHub MCP Server", "environment variable", "GITHUB_PERSONAL_ACCESS_TOKEN", "Authentication required.", "additional configuration"} {
		if !contains(detail, want) {
			t.Errorf("MCP picker GitHub detail view missing connector metadata %q: %q", want, detail)
		}
	}
}

func contains(s, want string) bool {
	return strings.Contains(s, want)
}
